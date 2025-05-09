import irc.bot
import irc.connection
import ssl
import threading
import time
import logging
import openai
from models import BotSettings, AISettings, ChannelManagementSettings
from extensions import app, db, socketio
from datetime import datetime, timedelta
from url_watcher import URLWatcher
from module_loader import ModuleLoader
from ai_utils import get_ai_response
import re

logger = logging.getLogger(__name__)

class Channel:
    """A class to represent an IRC channel and its users."""
    def __init__(self, name):
        self.name = name
        self.users = {}  # {nick: mode}
        logger.info(f"Created new Channel instance for {name}")
        
    def add_user(self, nick, mode=""):
        """Add a user to the channel with their mode."""
        logger.info(f"Adding user {nick} with mode {mode} to channel {self.name}")
        self.users[nick] = mode
        
    def remove_user(self, nick):
        """Remove a user from the channel."""
        logger.info(f"Removing user {nick} from channel {self.name}")
        if nick in self.users:
            del self.users[nick]
        
    def get_users(self):
        """Return a list of dicts: {nick, mode}."""
        users = [
            {"nick": nick, "mode": mode}
            for nick, mode in self.users.items()
        ]
        logger.info(f"Getting users for channel {self.name}: {users}")
        return users

class Conversation:
    """A class to track conversation state with a user."""
    def __init__(self, user, channel):
        self.user = user
        self.channel = channel
        self.last_interaction = datetime.now()
        self.messages = []
    
    def is_active(self):
        """Check if the conversation is still active (within 1 minute)."""
        return datetime.now() - self.last_interaction < timedelta(minutes=1)
    
    def update(self):
        """Update the last interaction time."""
        self.last_interaction = datetime.now()

class IRCBouncer(irc.bot.SingleServerIRCBot):
    def __init__(self, server, port, nick, username, realname, use_ssl=True):
        """Initialize the IRC bot."""
        self.server = server
        self.port = port
        self.nick = nick  # Store original nickname
        self.username = username
        self.realname = realname
        self.use_ssl = use_ssl
        self.is_connecting = False  # Add connection state tracking
        self.webchat_channels = {}  # Store channels to join (custom)
        self.conversations = {}  # Store active conversations
        self.webchat_messages = {}  # {channel: [ {nick, message, timestamp} ]}
        self.topics = {}  # {channel: topic}
        self.url_watcher = URLWatcher(self)
        self.module_loader = ModuleLoader(self)  # Initialize module loader
        self.message_history = {}  # Store message history for flood detection
        self.channel_settings = {}  # Store channel management settings
        self.load_channel_settings()
        
        logger.info(f"Initializing IRC bot with server={server}, port={port}, nick={nick}, ssl={use_ssl}")
        
        # Create IRC connection factory with SSL context that accepts invalid certificates
        factory = irc.connection.Factory()
        if use_ssl:
            logger.info("Creating SSL context with self-signed certificate support")
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            factory = irc.connection.Factory(wrapper=lambda sock: ssl_context.wrap_socket(sock))
        
        # Initialize the bot
        super().__init__([(server, port)], nick, realname, connect_factory=factory)
        
        # Start the bot in a separate thread AFTER initialization
        self.thread = threading.Thread(target=self._connect_and_run)
        self.thread.daemon = True
        self.thread.start()
        logger.info("Bot thread started")

    def get_chatgpt_response(self, message, conversation=None):
        """Get a response from the configured AI provider."""
        return get_ai_response(message, conversation)

    def send_message(self, connection, channel, message):
        """Send a message to a channel and handle webchat updates."""
        # Send the message to IRC
        connection.privmsg(channel, message)
        
        # Store message in webchat_messages
        if not hasattr(self, 'webchat_messages'):
            self.webchat_messages = {}
        if channel not in self.webchat_messages:
            self.webchat_messages[channel] = []
        
        # Add the message to history
        self.webchat_messages[channel].append({
            'nick': self.nick,
            'message': message,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
        
        # Limit history to last 100 messages
        if len(self.webchat_messages[channel]) > 100:
            self.webchat_messages[channel] = self.webchat_messages[channel][-100:]
        
        # Emit message to webchat
        socketio.emit('webchat_message', {
            'nick': self.nick,
            'message': message,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }, room=channel)

    def on_join(self, connection, event):
        """Called when the bot joins a channel or a user joins."""
        channel = event.target
        nick = event.source.nick if hasattr(event.source, 'nick') else None
        logger.info(f"Join event for channel {channel} by {nick}")
        
        if channel not in self.webchat_channels:
            logger.info(f"Creating new channel tracking for {channel}")
            self.webchat_channels[channel] = Channel(channel)
            # Request NAMES list for the new channel
            logger.info(f"Requesting NAMES list for {channel}")
            connection.names([channel])
            # Emit updated channel list to all clients
            logger.info("Emitting updated channel list")
            socketio.emit('webchat_channels', {
                'channels': list(self.webchat_channels.keys())
            })
        
        if nick:
            # Default to no mode on join
            logger.info(f"Adding user {nick} to channel {channel}")
            self.webchat_channels[channel].add_user(nick, "")
            # Broadcast updated userlist
            socketio.emit('webchat_users', {
                'users': self.webchat_channels[channel].get_users()
            }, room=channel)
            # Emit system join message
            timestamp = datetime.now().strftime('%H:%M:%S')
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {nick} has joined {channel}',
                'timestamp': timestamp
            }, room=channel)
        
        # Request topic
        logger.info(f"Requesting TOPIC for {channel}")
        connection.topic(channel)

    def kick(self, connection, channel, nick, reason):
        """Kick a user from a channel with a reason."""
        try:
            connection.kick(channel, nick, reason)
            logger.info(f"Kicked {nick} from {channel}: {reason}")
            # Emit system message to webchat
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {nick} has been kicked: {reason}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel)
        except Exception as e:
            logger.error(f"Failed to kick {nick} from {channel}: {e}")

    def on_pubmsg(self, connection, event):
        """Handle public messages."""
        channel = event.target
        nick = event.source.nick
        message = event.arguments[0]
        
        # Reload settings before checking
        self.reload_channel_settings()
        
        # Check for flood
        if self.check_flood(channel, nick):
            self.kick(connection, channel, nick, "Flooding detected")
            return
        
        # Check for caps
        if self.check_caps(channel, message):
            self.kick(connection, channel, nick, "Excessive caps usage")
            return
        
        logger.info(f"Received message in {channel} from {nick}: {message}")
        
        # Store message in webchat_messages
        if not hasattr(self, 'webchat_messages'):
            self.webchat_messages = {}
        if channel not in self.webchat_messages:
            self.webchat_messages[channel] = []
        self.webchat_messages[channel].append({
            'nick': nick,
            'message': message,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
        
        # Limit history to last 100 messages
        if len(self.webchat_messages[channel]) > 100:
            self.webchat_messages[channel] = self.webchat_messages[channel][-100:]
        
        # Emit message to webchat
        socketio.emit('webchat_message', {
            'nick': nick,
            'message': message,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }, room=channel)
        
        # Check if message is a command
        if message.startswith('!'):
            command = message[1:].split()[0].lower()
            args = message[1:].split()[1:]
            logger.info(f"Received command: {command} with args: {args}")
            
            # Pass command to module loader
            if hasattr(self, 'module_loader'):
                try:
                    self.module_loader.handle_command(connection, event, command, args, channel, nick)
                except Exception as e:
                    logger.error(f"Error handling command: {e}")
                    self.send_message(connection, channel, "Error executing command.")
        # Check if message mentions the bot's name
        elif self.nick.lower() in message.lower():
            # Get or create conversation
            conv_key = f"{nick}_{channel}"
            if conv_key not in self.conversations:
                self.conversations[conv_key] = Conversation(nick, channel)
            conversation = self.conversations[conv_key]
            conversation.update()
            
            # Get AI response within application context
            with app.app_context():
                response = self.get_chatgpt_response(message, conversation)
                if response:
                    # Use send_message to ensure proper IRC and webchat handling
                    self.send_message(connection, channel, response)
                    # Store bot's response in conversation
                    conversation.messages.append({"role": "assistant", "content": response})
                else:
                    logger.warning("No AI response received")
                    self.send_message(connection, channel, "I'm sorry, I couldn't generate a response at this time.")
        
        # URL Watcher integration
        self.url_watcher.handle_message(channel, nick, message)
        
        # Module handling
        self.module_loader.handle_message(connection, event)
        
        # Clean up expired conversations
        self._cleanup_conversations()

    def _cleanup_conversations(self):
        """Remove expired conversations."""
        current_time = datetime.now()
        expired = []
        for key, conv in self.conversations.items():
            if not conv.is_active():
                expired.append(key)
        for key in expired:
            del self.conversations[key]
            logger.info(f"Removed expired conversation: {key}")

    def _connect_and_run(self):
        """Connect to the server and start the bot."""
        if not self.is_connecting:
            self.is_connecting = True
            try:
                self.start()
            except Exception as e:
                logger.error(f"Error in bot thread: {e}")
                self.is_connecting = False

    def on_welcome(self, connection, event):
        """Called when the bot successfully connects to the server."""
        logger.info("Successfully connected to server")
        self.is_connecting = False
        
        # Update connection status in database
        with app.app_context():
            settings = BotSettings.query.first()
            if settings:
                settings.is_connected = True
                db.session.commit()
                logger.info("Updated connection status in database")
                
                # Emit status update with server information
                socketio.emit('status_update', {
                    'connected': True,
                    'server': settings.server,
                    'port': settings.port
                })
                
                # Set usermode +B
                logger.info("Setting usermode +B")
                connection.mode(connection.get_nickname(), "+B")
                
                # Identify with NickServ if password is set
                if settings.nickserv_password:
                    logger.info("Identifying with NickServ")
                    connection.privmsg("nickserv", f"IDENTIFY {settings.nickserv_password}")
                    # Wait a bit for identification to process
                    time.sleep(2)
                    
                    # If we're using an alternate nick and have the original nick stored, try to reclaim it
                    if hasattr(self, 'original_nick') and connection.get_nickname() != self.original_nick:
                        logger.info(f"Attempting to reclaim original nickname: {self.original_nick}")
                        connection.nick(self.original_nick)
                        # Clear the stored original nick after attempting to reclaim
                        delattr(self, 'original_nick')
                
                # Join channels
                if settings.channels:
                    channels = [chan.strip() for chan in settings.channels.split(',')]
                    for channel in channels:
                        if channel:
                            logger.info(f"Joining channel: {channel}")
                            print(f"[DEBUG] Assigning Channel({channel}) to self.webchat_channels[{channel}] in on_welcome")
                            self.webchat_channels[channel] = Channel(channel)
                            connection.join(channel)

    def on_disconnect(self, connection, event):
        """Called when the bot disconnects from the server."""
        logger.info("Disconnected from server")
        self.is_connecting = False
        
        # Update connection status in database
        with app.app_context():
            settings = BotSettings.query.first()
            if settings:
                settings.is_connected = False
                db.session.commit()
                logger.info("Updated connection status in database")
                
                # Emit status update
                socketio.emit('status_update', {
                    'connected': False,
                    'server': settings.server,
                    'port': settings.port
                })

    def on_nicknameinuse(self, connection, event):
        """Called when the bot's nickname is already in use."""
        if not self.is_connecting:  # Only handle if this isn't during initial connection
            logger.warning(f"Nickname {connection.get_nickname()} is in use, waiting to reclaim")
            return
            
        temp_nick = connection.get_nickname() + "_"
        logger.warning(f"Nickname {connection.get_nickname()} is in use during connection, temporarily using {temp_nick}")
        connection.nick(temp_nick)
        
        # Store the original nickname for later reclamation
        self.original_nick = connection.get_nickname()

    def on_part(self, connection, event):
        """Called when the bot or a user parts from a channel."""
        channel = event.target
        nick = event.source.nick if hasattr(event.source, 'nick') else None
        logger.info(f"Part event for channel {channel} by {nick}")
        
        if channel in self.webchat_channels and nick:
            logger.info(f"Removing user {nick} from channel {channel}")
            self.webchat_channels[channel].remove_user(nick)
            # Clear message history for this user in this channel
            if channel in self.message_history and nick in self.message_history[channel]:
                del self.message_history[channel][nick]
            # Broadcast updated userlist
            socketio.emit('webchat_users', {
                'users': self.webchat_channels[channel].get_users()
            }, room=channel)
            # Emit system part message
            timestamp = datetime.now().strftime('%H:%M:%S')
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {nick} has left {channel}',
                'timestamp': timestamp
            }, room=channel)
        
        if channel in self.webchat_channels and not self.webchat_channels[channel].get_users():
            logger.info(f"Removing empty channel {channel}")
            del self.webchat_channels[channel]

    def on_namreply(self, connection, event):
        """Called when receiving names list for a channel."""
        channel = event.arguments[1]
        logger.info(f"Received NAMES list for {channel}")
        logger.info(f"NAMES list arguments: {event.arguments}")
        
        # Ensure channel exists in our tracking
        if channel not in self.webchat_channels:
            logger.info(f"Creating new channel tracking for {channel}")
            self.webchat_channels[channel] = Channel(channel)
        
        # Clear existing users for this channel
        logger.info(f"Clearing existing users for {channel}")
        self.webchat_channels[channel].users.clear()
        
        # Add each user to the channel
        users = event.arguments[2].split()
        logger.info(f"Raw users list: {users}")
        logger.info(f"Adding {len(users)} users to {channel}")
        
        for nick in users:
            mode = ""
            if nick[0] in '~&@%+':
                mode = nick[0]
                nick = nick[1:]
            logger.info(f"Adding user {nick} with mode {mode} to {channel}")
            self.webchat_channels[channel].add_user(nick, mode)
        
        # Get current users and emit update
        current_users = self.webchat_channels[channel].get_users()
        logger.info(f"Current users in channel {channel}: {current_users}")
        logger.info(f"Emitting userlist update for {channel} with {len(current_users)} users")
        
        # Emit to all clients in the channel
        socketio.emit('webchat_users', {
            'users': current_users
        }, room=channel)

    def on_topic(self, connection, event):
        """Called when a channel topic is set or changed."""
        channel = event.target
        topic = event.arguments[0] if event.arguments else ''
        logger.info(f"Topic changed for {channel}: {topic}")
        
        # Store topic
        if not hasattr(self, 'topics'):
            self.topics = {}
        self.topics[channel] = topic
        
        # Emit topic update to all clients in the channel
        socketio.emit('webchat_topic', {
            'topic': topic,
            'channel': channel
        }, room=channel)

    def on_currenttopic(self, connection, event):
        """Called when receiving current topic for a channel."""
        channel = event.arguments[0]
        topic = event.arguments[1] if len(event.arguments) > 1 else ''
        logger.info(f"Current topic for {channel}: {topic}")
        
        # Store topic
        if not hasattr(self, 'topics'):
            self.topics = {}
        self.topics[channel] = topic
        
        # Emit topic update to all clients in the channel
        socketio.emit('webchat_topic', {
            'topic': topic,
            'channel': channel
        }, room=channel)

    def reconnect(self):
        """Reconnect to the IRC server with new settings."""
        if self.is_connecting:
            logger.info("Already connecting/connected, skipping reconnect")
            return
            
        logger.info("Initiating reconnection...")
        self.disconnect("Reconnecting with new settings")
        time.sleep(2)  # Wait before reconnecting
        
        # Get new settings from database within application context
        with app.app_context():
            settings = BotSettings.query.first()
            if settings:
                logger.info(f"Reconnecting with new settings: {settings.server}:{settings.port}")
                self.server = settings.server
                self.port = settings.port
                self.nick = settings.nick
                self.username = settings.username
                self.realname = settings.realname
                self.use_ssl = settings.use_ssl
                
                # Create IRC connection factory with SSL context that accepts invalid certificates
                factory = irc.connection.Factory()
                if self.use_ssl:
                    logger.info("Creating SSL context with self-signed certificate support")
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    factory = irc.connection.Factory(wrapper=lambda sock: ssl_context.wrap_socket(sock))
                
                # Update connection factory
                self._connect_factory = factory
                
                # Connect to server
                self.is_connecting = True
                try:
                    self.connect(self.server, self.port, self.nick, connect_factory=factory)
                except irc.client.ServerConnectionError as e:
                    logger.error(f"Failed to connect: {e}")
                    self.is_connecting = False 

    def disconnect(self):
        """Disconnect from the server."""
        if self.connection:
            self.connection.disconnect()
            self.connection = None
            self.is_connecting = False
            self.webchat_channels.clear()
            self.conversations.clear() 

    def is_actually_connected(self):
        """Check if the bot is actually connected to the IRC server."""
        try:
            if not self.connection:
                return False
            # Try to get the current nickname - if we can't, we're not connected
            self.connection.get_nickname()
            return True
        except Exception:
            return False 

    def now_str(self):
        return datetime.now().strftime('%H:%M:%S')

    def load_channel_settings(self):
        """Load channel management settings from database."""
        with app.app_context():
            settings = ChannelManagementSettings.query.all()
            for setting in settings:
                self.channel_settings[setting.channel] = {
                    'is_enabled': setting.is_enabled,
                    'flood_threshold': setting.flood_threshold,
                    'flood_timeframe': setting.flood_timeframe,
                    'caps_percentage': setting.caps_percentage
                }
            logger.info(f"Loaded channel settings: {self.channel_settings}")

    def reload_channel_settings(self):
        """Reload channel management settings from database."""
        self.channel_settings.clear()  # Clear existing settings
        with app.app_context():
            settings = ChannelManagementSettings.query.all()
            for setting in settings:
                self.channel_settings[setting.channel] = {
                    'is_enabled': setting.is_enabled,
                    'flood_threshold': setting.flood_threshold,
                    'flood_timeframe': setting.flood_timeframe,
                    'caps_percentage': setting.caps_percentage
                }
            logger.info(f"Reloaded channel settings: {self.channel_settings}")

    def check_flood(self, channel, nick):
        """Check if a user is flooding in a channel."""
        if not self.channel_settings.get(channel, {}).get('is_enabled', False):
            return False

        settings = self.channel_settings[channel]
        threshold = settings.get('flood_threshold', 5)
        timeframe = settings.get('flood_timeframe', 10)

        # Initialize message history for this channel if not exists
        if channel not in self.message_history:
            self.message_history[channel] = {}

        # Initialize message history for this user if not exists
        if nick not in self.message_history[channel]:
            self.message_history[channel][nick] = []

        # Add current message timestamp
        current_time = datetime.now()
        self.message_history[channel][nick].append(current_time)

        # Remove messages outside the timeframe
        cutoff_time = current_time - timedelta(seconds=timeframe)
        self.message_history[channel][nick] = [
            t for t in self.message_history[channel][nick]
            if t > cutoff_time
        ]

        # Check if user has exceeded the threshold
        if len(self.message_history[channel][nick]) > threshold:
            logger.warning(f"Flood detected from {nick} in {channel}")
            return True

        return False

    def check_caps(self, channel, message):
        """Check if a message contains excessive caps."""
        if not self.channel_settings.get(channel, {}).get('is_enabled', False):
            return False

        settings = self.channel_settings[channel]
        caps_percentage = settings.get('caps_percentage', 70)

        # Count uppercase letters
        caps_count = sum(1 for c in message if c.isupper())
        total_letters = sum(1 for c in message if c.isalpha())

        # Skip if no letters or if message is too short
        if total_letters < 5:
            return False

        # Calculate percentage of caps
        caps_ratio = (caps_count / total_letters) * 100

        if caps_ratio > caps_percentage:
            logger.warning(f"Excessive caps detected in {channel}: {caps_ratio}%")
            return True

        return False

    def on_kick(self, connection, event):
        """Called when a user is kicked from a channel."""
        channel = event.target
        kicked_nick = event.arguments[0]
        reason = event.arguments[1] if len(event.arguments) > 1 else "No reason given"
        kicker = event.source.nick
        
        logger.info(f"Kick event in {channel}: {kicker} kicked {kicked_nick}: {reason}")
        
        if channel in self.webchat_channels:
            # Remove user from channel
            self.webchat_channels[channel].remove_user(kicked_nick)
            # Clear message history for this user in this channel
            if channel in self.message_history and kicked_nick in self.message_history[channel]:
                del self.message_history[channel][kicked_nick]
            # Broadcast updated userlist
            socketio.emit('webchat_users', {
                'users': self.webchat_channels[channel].get_users()
            }, room=channel)
            # Emit system kick message
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {kicked_nick} was kicked by {kicker}: {reason}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel)

    def on_mode(self, connection, event):
        """Called when channel modes are changed."""
        channel = event.target
        modes = event.arguments[0]
        args = event.arguments[1:]
        source = event.source.nick
        
        logger.info(f"Mode change in {channel} by {source}: {modes} {args}")
        
        # Handle ban modes (+b/-b)
        if 'b' in modes:
            mode_type = modes[0]  # + or -
            if mode_type == '+':
                # Ban added
                ban_mask = args[0]
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {source} banned {ban_mask}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel)
            else:
                # Ban removed
                ban_mask = args[0]
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {source} unbanned {ban_mask}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel)

    def on_ban(self, connection, event):
        """Called when a user is banned from a channel."""
        channel = event.target
        ban_mask = event.arguments[0]
        banner = event.source.nick
        
        logger.info(f"Ban event in {channel}: {banner} banned {ban_mask}")
        
        # Check if the banned user is in our channel
        if channel in self.webchat_channels:
            # Try to match the ban mask to a user in the channel
            for nick in list(self.webchat_channels[channel].users.keys()):
                if self._match_ban_mask(nick, ban_mask):
                    # Remove user from channel
                    self.webchat_channels[channel].remove_user(nick)
                    # Broadcast updated userlist
                    socketio.emit('webchat_users', {
                        'users': self.webchat_channels[channel].get_users()
                    }, room=channel)
                    # Emit system ban message
                    socketio.emit('webchat_message', {
                        'nick': '',
                        'message': f'* {nick} was banned by {banner}',
                        'timestamp': datetime.now().strftime('%H:%M:%S')
                    }, room=channel)

    def _match_ban_mask(self, nick, ban_mask):
        """Check if a nickname matches a ban mask."""
        # Convert ban mask to regex pattern
        pattern = ban_mask.replace('*', '.*').replace('?', '.')
        try:
            return bool(re.match(pattern, nick, re.IGNORECASE))
        except re.error:
            return False

    def on_quit(self, connection, event):
        """Called when a user quits the server."""
        nick = event.source.nick
        reason = event.arguments[0] if event.arguments else "No reason given"
        
        logger.info(f"Quit event: {nick} quit: {reason}")
        
        # Remove user from all channels
        for channel in self.webchat_channels.values():
            if nick in channel.users:
                channel.remove_user(nick)
                # Clear message history for this user in this channel
                if channel.name in self.message_history and nick in self.message_history[channel.name]:
                    del self.message_history[channel.name][nick]
                # Broadcast updated userlist
                socketio.emit('webchat_users', {
                    'users': channel.get_users()
                }, room=channel.name)
                # Emit system quit message
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {nick} has quit: {reason}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel.name)

    def on_nick(self, connection, event):
        """Called when a user changes their nickname."""
        old_nick = event.source.nick
        # Use event.arguments[0] if present, else event.target (for nick changes)
        new_nick = event.arguments[0] if event.arguments else event.target

        if not new_nick:
            logger.error("Nick change event missing new nickname (arguments and target both empty)")
            return

        logger.info(f"Nick change: {old_nick} -> {new_nick}")

        for channel in self.webchat_channels.values():
            if old_nick in channel.users:
                mode = channel.users[old_nick]
                channel.remove_user(old_nick)
                channel.add_user(new_nick, mode)
                # Emit system nick change message
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {old_nick} is now known as {new_nick}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel.name)

        # If the bot's nickname changed, update it
        if old_nick == self.nick:
            self.nick = new_nick
            logger.info(f"Bot's nickname changed to {new_nick}")

        # Force userlist refresh for all channels
        self.emit_all_userlists()
        logger.info(f"Userlist refreshed after nick change: {old_nick} -> {new_nick}")

    def emit_all_userlists(self):
        for channel in self.webchat_channels.values():
            socketio.emit('webchat_users', {
                'users': channel.get_users()
            }, room=channel.name)

    def on_glined(self, connection, event):
        """Called when a user is G-lined (global ban)."""
        user = event.arguments[0]
        reason = event.arguments[1] if len(event.arguments) > 1 else "No reason given"
        
        logger.info(f"G-line event: {user} was G-lined: {reason}")
        
        # Remove user from all channels
        for channel in self.webchat_channels.values():
            if user in channel.users:
                channel.remove_user(user)
                # Broadcast updated userlist
                socketio.emit('webchat_users', {
                    'users': channel.get_users()
                }, room=channel.name)
                # Emit system G-line message
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {user} was G-lined: {reason}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel.name)

    def on_zlined(self, connection, event):
        """Called when a user is Z-lined (IP ban)."""
        ip = event.arguments[0]
        reason = event.arguments[1] if len(event.arguments) > 1 else "No reason given"
        
        logger.info(f"Z-line event: {ip} was Z-lined: {reason}")
        
        # Note: We can't directly match IPs to users, but we can notify channels
        for channel in self.webchat_channels.values():
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* IP {ip} was Z-lined: {reason}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel.name)

    def on_klined(self, connection, event):
        """Called when a user is K-lined (server ban)."""
        user = event.arguments[0]
        reason = event.arguments[1] if len(event.arguments) > 1 else "No reason given"
        
        logger.info(f"K-line event: {user} was K-lined: {reason}")
        
        # Remove user from all channels
        for channel in self.webchat_channels.values():
            if user in channel.users:
                channel.remove_user(user)
                # Broadcast updated userlist
                socketio.emit('webchat_users', {
                    'users': channel.get_users()
                }, room=channel.name)
                # Emit system K-line message
                socketio.emit('webchat_message', {
                    'nick': '',
                    'message': f'* {user} was K-lined: {reason}',
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }, room=channel.name)

    def on_invite(self, connection, event):
        """Called when a user is invited to a channel."""
        invited_nick = event.arguments[0] if event.arguments else None
        channel = event.target if hasattr(event, 'target') else None
        inviter = event.source.nick if hasattr(event.source, 'nick') else str(event.source)
        logger.info(f"Invite event: {inviter} invited {invited_nick} to {channel}")
        if channel:
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {inviter} invited {invited_nick} into {channel}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel)

    def on_ctcp(self, connection, event):
        """Handle CTCP events, including ACTION (/me)."""
        if event.arguments and event.arguments[0].upper() == 'ACTION':
            channel = event.target
            nick = event.source.nick
            action_message = event.arguments[1] if len(event.arguments) > 1 else ''
            logger.info(f"CTCP ACTION in {channel} from {nick}: {action_message}")
            # Emit as a system message in webchat
            socketio.emit('webchat_message', {
                'nick': '',
                'message': f'* {nick} {action_message}',
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel)

    def on_pubnotice(self, connection, event):
        """Handle public notices, including invite messages."""
        channel = event.target
        message = event.arguments[0] if event.arguments else ''
        logger.info(f"PubNotice in {channel}: {message}")
        # Look for invite pattern
        if 'invited' in message and 'into the channel' in message:
            # Example: '*** TheGrimPody invited Ponysauce into the channel'
            socketio.emit('webchat_message', {
                'nick': '',
                'message': message.strip('* '),
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, room=channel) 