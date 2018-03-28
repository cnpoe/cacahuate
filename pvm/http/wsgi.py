from flask import Flask
from flask_coralillo import Coralillo
import os
import time

from pvm.http.forms import bind_forms
from pvm.models import bind_models

# The flask application
app = Flask(__name__)
app.config.from_object('settings')
app.config.from_envvar('PVM_SETTINGS', silent=True)

# Timezone
os.environ['TZ'] = app.config.get('TIMEZONE', 'UTC')
time.tzset()

# Bind the database
cora = Coralillo(app)
bind_forms(cora._engine)
bind_models(cora._engine)

# Url converters
import pvm.http.converters

# Views
import pvm.http.views.api
import pvm.http.views.auth

# Error handlers
import pvm.http.error_handlers