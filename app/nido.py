import json
from numbers import Number
from functools import wraps
from flask import Flask, request, session, g, redirect, url_for, abort, render_template, flash
from werkzeug.routing import BaseConverter
from lib.Nido import Sensor, LocalWeather, Config, Controller, Status, ControllerError, ConfigError, Mode

config = Config()
# DEBUG = config.get_config()['flask']['debug']
DEBUG = True
SECRET_KEY = config.get_config()['flask']['secret_key']
PUBLIC_API_SECRET = config.get_config()['flask']['public_api_secret']
GOOGLE_API_KEY = config.get_config()['google']['api_key']

app = Flask(__name__)
app.config.from_object(__name__)

# JSON response object
#
class JSONResponse():
    def __init__(self):
        self.status = 200
        self.data = {
                'version': config.get_version()
                }
        return

    def get_flask_response(self):
        response = app.make_response(json.dumps(self.data))
        response.headers['Content-Type'] = 'application/json'
        response.status_code = self.status
        return response

# Helper function to validate JSON in requests
# A list of tuples is passed in of the form ( 'name', type )
# Where:
#        'name' is the key we expect in the dict
#        type is a type we can compare the corresponding value to using isinstance()
def validate_json_req(req_data, valid):
    # Make sure we have JSON in the body first
    if (req_data == None) or (req_data == {}):
        return False

    # Request data can't have more entries than the validation set
    if len(valid.keys()) < len(req_data.keys()):
        print 'Bad length'
        return False

    # Check that each element in the request data is valid
    for setting in req_data:
        if setting not in valid:
            print 'Setting name is not in valid dict'
            return False
        elif not isinstance(req_data[setting], valid[setting]):
            print 'Setting value is not instance of valid type'
            print 'setting: {}, type: {}'.format(req_data[setting], type(req_data[setting]))
            print 'valid type: {}'.format(valid[setting])
            return False

    # No tests failed
    return True

# Helper function to set config
def set_config_helper(resp, cfg):
    if config.update_config(cfg):
        resp.data['message'] = 'Configuration updated successfully.'
        resp.data['config'] = cfg
        # Send signal to daemon, if running, to trigger update
        try:
            Controller().signal_daemon()
        except Exception as e:
            resp.data['error'] = 'Server error signalling daemon: {}'.format(e)
    else:
        resp.data['error'] = 'Invalid configuration setting(s).'
        resp.data['config'] = cfg['config']
    return resp

# Decorator for routes that require a session cookie
#
def require_session(route):
    @wraps(route)
    def check_session(*args, **kwargs):
        if not session.get('logged_in'):
            abort(403)
        else:
            return route(*args, **kwargs)

    return check_session

# Decorator for API routes to verify that client supplied a secret in the request body
#
def require_secret(route):
    @wraps(route)
    def check_secret(*args, **kwargs):
        # Validate that the JSON in the body has a secret
        validation = { 'secret': basestring }
        # JSON request data
        req_data = request.get_json()
        # Prepare a JSONResponse in case we need it
        resp = JSONResponse()

        if validate_json_req(req_data, validation):
            if req_data['secret'] == PUBLIC_API_SECRET:
                return route(*args, **kwargs)
            else:
                resp.data['error'] = 'Invalid secret.'
                resp.status = 401
        else:
            resp.data['error'] = 'JSON in request was invalid.'
            resp.status = 400

        return resp.get_flask_response()

    return check_secret

# Custom URL converter to allow use of regex
# Source: https://stackoverflow.com/questions/5870188/does-flask-support-regular-expressions-in-its-url-routing
#
class RegexConverter(BaseConverter):
    def __init__(self, url_map, *items):
        super(RegexConverter, self).__init__(url_map)
        self.regex = items[0]

# Register custom converter with Flask
app.url_map.converters['regex'] = RegexConverter

# Application routes
#   All routes are POST-only and should only return JSON.
#   The / route only serves to return the React-based UI
#
@app.route('/set_config', methods=['POST'])
@require_session
def set_config():
    # Initialize response object
    resp = JSONResponse()
    new_cfg = request.get_json()
        
    # Expect to receive a json dict with one or more of the following pairs
    # TODO: Improve validation
    #       eg. location should be a list of only two numbers
    #       eg. modes_available be a list of lists, each with only two values
    validation = {
        'location': list,
        'celsius': bool,
        'modes_available': list,
        'mode_set': basestring,
        'set_temperature': Number
        }

    # Update local configuration with user data
    if validate_json_req(new_cfg, validation):
        resp = set_config_helper(resp, new_cfg)
    else:
        resp.data['error'] = 'JSON in request was invalid.'
        resp.status = 400

    return resp.get_flask_response()

@app.route('/get_config', methods=['POST'])
@require_session
def get_config():
    # Initialize response dict
    resp = JSONResponse()
    # Get config
    cfg = config.get_config()
    # Check that settings config section exists
    if 'config' in cfg:
        resp.data['config'] = cfg['config']
    else:
        resp.data['error'] = 'Unable to retrieve config schema.'

    return resp.get_flask_response()

@app.route('/get_state', methods=['POST'])
@require_session
def get_state():
    # Initialize response object
    resp = JSONResponse()
    resp.data['state'] = {}
    resp.data['error'] = []
    
    try:
        state = Controller().get_status()
    except ControllerError as e:
        err_msg = 'Exception getting current state from controller: {}'.format(str(e))
        resp.data['error'].append(err_msg)
    else:
        # state = Heating / Cooling / Off
        nidoState = { 'status': Status(state).name }
        resp.data['state'].update(nidoState)
        
    # Returns a JSON dict with an 'error' key on error
    # On success, returns a JSON dict with a 'conditions' key
    sensor_data = Sensor().get_conditions()
    if 'error' in sensor_data:
        resp.data['error'].append(sensor_data['error'])
    else:
        resp.data['state'].update(sensor_data)

    daemonState = { 'daemon_running': Controller().daemon_running() }
    resp.data['state'].update(daemonState)

    if len(resp.data['error']) == 0:
        del resp.data['error']
    return resp.get_flask_response()

@app.route('/get_weather', methods=['POST'])
def get_weather():
    resp = JSONResponse()

    # Any errors will be passed through
    # The receiving application should note the retrieval_age value as necessary
    # TODO: Support caching built into the LocalWeather() object by storing the object in the session
    #       Needs to be a serializable object (see Flask documentation)
    resp.data = LocalWeather().get_conditions()

    return resp.get_flask_response()

@app.route('/login', methods=['POST'])
def login():
    # Intialize response object
    resp = JSONResponse()
    # Get config
    cfg = config.get_config()
    
    if 'logged_in' in session:
        resp.data['message'] = 'User already logged in.'
        resp.data['username'] = session['username']
        resp.data['logged_in'] = True
    else:
        if request.form['username'] != cfg['flask']['username'] or request.form['password'] != cfg['flask']['password']:
            resp.data['error'] = 'Incorrect login credentials.'
            resp.data['logged_in'] = False
        else:
            # Set (implicit create) session cookie (HTTP only) which all endpoints will check for
            session['logged_in'] = True
            session['username'] = request.form['username']
            # Default session lifetime is 31 days
            session.permanent = True
            resp.data['logged_in'] = True
            resp.data['message'] = 'User has been logged in.'

    return resp.get_flask_response()

@app.route('/logout', methods=['POST'])
def logout():
    # Initialize response object
    resp = JSONResponse()
    if 'logged_in' in session:
        resp.data['message'] = 'User has been logged out.'
        resp.data['username'] = session['username']
        resp.data['logged_in'] = False
        # Note that clear() removes every key from the session dict, which causes the cookie to be destroyed
        session.clear()
    else:
        resp.data['error'] = 'User not logged in.'
        resp.data['logged_in'] = False
    return resp.get_flask_response()

@app.route('/')
def render_ui():
    return render_template('index.html', google_api_key=GOOGLE_API_KEY)

# Public API routes
#   Secured by a pre-shared secret key in the request body

# Endpoint to accept a new mode setting.
# Only setting one of the valid configured modes is possible.
#
@app.route('/api/set_mode/<string:set_mode>', methods=['POST'])
@require_secret
def api_set_mode(set_mode):
    # Initialize response object
    resp = JSONResponse()
    cfg = config.get_config()
    # Get list of available modes
    modes = config.list_modes(cfg['config']['modes_available'])
    valid_mode = False

    for mode in modes:
        if mode.upper() == set_mode.upper():
            cfg['config']['mode_set'] = mode
            resp = set_config_helper(resp, cfg)
            valid_mode = True

    if not valid_mode:
        resp.data['error'] = 'Invalid mode.'
        resp.status = 400

    return resp.get_flask_response()

# Endpoint to accept a new set temperature in either Celsius or Fahrenheit.
# The first regex accepts either integer or floating point numbers.
#
@app.route('/api/set_temp/<regex("(([0-9]*)(\.([0-9]+))?)"):temp>/<regex("[cCfF]"):scale>', methods=['POST'])
@require_secret
def api_set_temp(temp, scale):
    # Initialize response object
    resp = JSONResponse()
    cfg = config.get_config()
    # Convert temp to float
    temp = float("{0:.1f}".format(float(temp)))

    scale = scale.upper()
    if scale == 'C':
        cfg['config']['set_temperature'] = temp
        resp = set_config_helper(resp, cfg)
    elif scale == 'F':
        # The following conversion duplicates the logic in nido.js
        celsius_temp = (temp - 32) * 5 / 9
        celsius_temp = round(celsius_temp * 10) / 10
        celsius_temp = float("{0:.1f}".format(celsius_temp))
        cfg['config']['set_temperature'] = celsius_temp
        resp = set_config_helper(resp, cfg)

    return resp.get_flask_response()

if __name__ == '__main__':
    # We're using an adhoc SSL context, which is not considered secure by browsers
    # because it invokes a self-signed certificate.
    app.run(host='0.0.0.0', port=config.get_config()['flask']['port'], ssl_context='adhoc', threaded=False)
