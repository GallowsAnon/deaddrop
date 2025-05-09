from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_socketio import SocketIO
 
app = Flask(__name__)
db = SQLAlchemy()
socketio = SocketIO(async_mode='threading')
login_manager = LoginManager()
login_manager.login_view = 'login' 