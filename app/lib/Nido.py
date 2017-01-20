import requests, json, time, yaml, os, signal
from enum import Enum
import RPi.GPIO as GPIO
from Adafruit_BME280 import *

# Enums:
#   Mode
#   Status
#   FormTypes
# Weather data:
#   LocalWeather
# Hardware data:
#   Sensor
# Hardware control:
#   ControllerError
#   Controller
# Configuration:
#   Config

class Mode(Enum):
    Off = 0
    Heat = 1
    Cool = 2
    Heat_Cool = 3

class Status(Enum):
    Off = 0
    Heating = 1
    Cooling = 2

class FormTypes(Enum):
    text = 0
    password = 1
    checkbox = 2
    radio = 3
    select = 4
    textarea = 5

class Sensor():
    def __init__(self, mode=BME280_OSAMPLE_8):
        try:
            self.sensor = BME280(mode)
        except:
            raise

    def get_conditions(self):
        # Initialize response dict
        resp = {}

        # Get sensor data
        try:
            conditions = {
                'temp_c': self.sensor.read_temperature(),
                'pressure_mb': self.sensor.read_pressure() / 100,
                'relative_humidity': self.sensor.read_humidity()
                }
        except Exception as e:
            resp['error'] = 'Exception getting sensor data: {} {}'.format(type(e), str(e))
        else:
            resp['conditions'] = conditions
        
        return resp

class LocalWeather():
    def __init__(self, zipcode=None, location=None):
        if zipcode:
            self.set_zipcode(zipcode)
        else:
            self.zipcode = None
        if location:
            self.set_location(location)
        else:
            self.location = None
        self.conditions = None
        # Unix time of last request to implement basic caching
        self.last_req = 0
        # Cache expiry period in seconds
        # 900 == 15 minutes
        self._CACHE_EXPIRY = 900
        self.api_key = Config().get_config()['wunderground']['api_key']

    def set_zipcode(self, zipcode):
        if not isinstance(zipcode, int):
            raise TypeError
        self.zipcode = zipcode
        self.conditions = None
        return

    def set_location(self, location):
        if not isinstance(location, tuple):
            raise TypeError
        self.location = location
        self.conditions = None
        return

    def get_conditions(self):
        # Initialize response dict
        resp = {}
        # How long since last retrieval?
        interval = int(time.time()) - self.last_req
        # System clock must have changed. Make cache stale.
        if interval < 0:
            interval = self._CACHE_EXPIRY
        # We've never made a request.
        if self.last_req == 0:
            interval = -1

        # If we made a request within caching period and have a cached result, use that instead
        if self.conditions and (interval < self._CACHE_EXPIRY) and (interval >= 0):
            resp['weather'] = self.conditions
            resp['retrieval_age'] = interval
            return resp

        # Determine location query type
        # API documentation here: http://api.wunderground.com/weather/api/d/docs?d=data/index
        # Prefer lat,long over zipcode
        if (self.location is None) and (self.zipcode is None):
            query = 'autoip'
        elif self.location:
            query = ','.join(map(str, self.location))
        elif self.zipcode:
            query = self.zipcode

        # Set up Wunderground API request
        request_url = 'http://api.wunderground.com/api/{}/conditions/q/{}.json'.format(self.api_key, query)
        try:
            r = requests.get(request_url)
        except Exception as e:
            # Making the request failed
            resp['error'] = 'Error retrieving local weather: {}'.format(e)
            # If we have any cached conditions (regardless of age), return them
            if self.conditions:
                resp['weather'] = self.conditions
                resp['retrieval_age'] = interval
        else:
            # Request was successful, parse response JSON
            r_json = r.json()
            try:
                observation_data = r_json['current_observation']
            except KeyError:
                # 'current_observation' data is missing, look for error description in response
                try:
                    api_error = r_json['response']['error']
                except KeyError:
                    # Error description was missing, return full response data instead
                    resp['error'] = 'Unknown Wunderground API error. Response data: ' + str(r_json)
                    # If we have any cached conditions (regardless of age), return them
                    if self.conditions:
                        resp['weather'] = self.conditions
                        resp['retrieval_age'] = interval
                else:
                    # Return error type and description from Wunderground API
                    resp['error'] = 'Wunderground API error (' + api_error['type'] + '): ' + api_error['description']
                    # If we have any cached conditions (regardless of age), return them
                    if self.conditions:
                        resp['weather'] = self.conditions
                        resp['retrieval_age'] = interval
            else:
                # 'current_observation' data was available, parse conditions
                try:
                    self.conditions = {
                            'location': {
                                'full': observation_data['display_location']['full'],
                                'city': observation_data['display_location']['city'],
                                'state': observation_data['display_location']['state'],
                                'zipcode': observation_data['display_location']['zip'],
                                'country': observation_data['display_location']['country'],
                                'coordinates': {
                                    'latitude': observation_data['display_location']['latitude'],
                                    'longitude': observation_data['display_location']['longitude']
                                    },
                                },
                            'temp_c': "{}".format(observation_data['temp_c']),
                            'relative_humidity': observation_data['relative_humidity'],
                            'pressure_mb': observation_data['pressure_mb'],
                            'condition': {
                                'description': observation_data['weather'],
                                'icon_url': observation_data['icon_url']
                                }
                            }
                except KeyError as e:
                    # Something changed in the response format, generate an error
                    resp['error'] = 'Error parsing Wunderground API data: {}' + str(e)
                else:
                    # Otherwise, if we successfully got here, everything actually worked!
                    resp['weather'] = self.conditions
                    # Reset retrieval time and update response
                    self.last_req = int(time.time())
                    resp['retrieval_age'] = 0
        
        return resp

class ControllerError(Exception):
    """Exception class for errors generated by the controller"""

    def __init__(self, msg):
        self.msg = msg
        return

    def __str__(self):
        return repr(self.msg)

# TODO: Record start/stop times for all heating/cooling events
class Controller():
    """This is the controller code that determines whether the heating / cooling system
    should be enabled based on the thermostat set point."""

    def __init__(self):
        # Get Nido configuration
        try:
            self.cfg = Config()
            config = self.cfg.get_config()
        except:
            raise
        else:
            self._HEATING = config['GPIO']['heat_pin']
            self._COOLING = config['GPIO']['cool_pin']

        # Set up the GPIO pins
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._HEATING, GPIO.OUT)
            GPIO.setup(self._COOLING, GPIO.OUT)
        except:
            raise

        return

    def get_status(self):
        if (GPIO.input(self._HEATING) and GPIO.input(self._COOLING)):
            self.shutdown()
            raise ControllerError('Both heating and cooling pins were enabled. Both pins disabled as a precaution.')
        elif GPIO.input(self._HEATING):
            return Status.Heating.value
        elif GPIO.input(self._COOLING):
            return Status.Cooling.value
        else:
            return Status.Off.value

    def _enable_heating(self, status, temp, set_temp, hysteresis):
        if ( (temp + hysteresis) < set_temp ):
            GPIO.output(self._HEATING, True)
            GPIO.output(self._COOLING, False)
        elif ( (temp < set_temp) and (status is Status.Heating) ):
            GPIO.output(self._HEATING, True)
            GPIO.output(self._COOLING, False)
        return
    
    def _enable_cooling(self, status, temp, set_temp, hysteresis):
        if ( (temp + hysteresis) > set_temp ):
            GPIO.output(self._HEATING, False)
            GPIO.output(self._COOLING, True)
        elif ( (temp > set_temp) and (status is Status.Cooling) ):
            GPIO.output(self._HEATING, False)
            GPIO.output(self._COOLING, True)
        return
    
    def shutdown(self):
        GPIO.output(self._HEATING, False)
        GPIO.output(self._COOLING, False)
        return

    def update(self):
        try:
            config = self.cfg.get_config()
        except:
            raise
        else:
            try:
                mode = config['settings']['mode']
                status = self.get_status()
                temp = Sensor.get_conditions()['conditions']['temp_c']
                set_temp = config['settings']['set_temperature']
                hysteresis = config['behavior']['hysteresis']
            except KeyError as e:
                self.shutdown()
                raise ControllerError('Encountered KeyError getting current Nido status: {}'.format(e))
            except:
                self.shutdown()
                raise

        if mode is Mode.Off:
            self.shutdown()
        elif mode is Mode.Heat:
            if temp < set_temp:
                self._enable_heating(status, temp, set_temp, hysteresis)
            else:
                self.shutdown()
        else:
            # Additional modes can be enabled in future, eg. Mode.Cool, Mode.Heat_Cool
            self.shutdown()

        return

    @staticmethod
    def signal_daemon(pid_file):
        try:
            f = open(pid_file)
        except IOError:
            raise
        else:
            pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR1)
        finally:
            f.close()

class Config():
    def __init__(self):
        self._CONFIG = '/home/pi/nido/app/cfg/config.yaml'
        self._SCHEMA_VERSION = '1.0'
        self._SCHEMA = {
                'GPIO': {
                    'heat_pin': {
                        'required': True
                        },
                    'cool_pin': {
                        'required': True
                        },
                    },
                'behavior': {
                    'hysteresis': {
                        'required': False,
                        'default': 0.6
                        }
                    },
                'flask': {
                    'port': {
                        'required': True
                        },
                    'debug': {
                        'required': False,
                        'default': False
                        },
                    'secret_key': {
                        'required': True
                        },
                    'username': {
                        'required': True
                        },
                    'password': {
                        'required': True
                        }
                    },
                'wunderground': {
                    'api_key': {
                        'required': True
                        }
                    },
                'config': {
                    'location': {
                        'form_data': (FormTypes.text.name, None),
                        'label': 'Location',
                        'required': False
                        },
                    'celsius': {
                        'required': False,
                        'default': True
                        },
                    'modes_available': {
                        'form_data': (FormTypes.checkbox.name, [ [ Mode.Heat.name, True ], [ Mode.Cool.name, False ] ]),
                        'label': 'Available modes',
                        'required': False,
                        'default': [ Mode.Heat.name ]
                        },
                    'set_temperature': {
                        'required': False,
                        'default': 21
                        },
                    'modes': {
                        'required': False,
                        'default': [ Mode.Off.name, Mode.Heat.name ]
                        }
                    },
                'daemon': {
                    'pid_file': {
                        'required': True
                        },
                    'log_file': {
                        'required': True
                        },
                    'work_dir': {
                        'required': True
                        },
                    'poll_interval': {
                        'required': False,
                        'default': 300
                        }
                    }
                }
        return
    
    def get_config(self):
        with open(self._CONFIG, 'r') as f:
            return yaml.load(f)

    def set_config(self, config):
        with open(self._CONFIG, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, indent=4)
        return

    def get_schema(self, section):
        return self._SCHEMA[section]

    def get_version(self):
        return self._SCHEMA_VERSION

    def validate(self):
        config = self.get_config()

        # Iterate through schema and check required flag against loaded config
        for section in self._SCHEMA:
            for setting in self._SCHEMA[section]:
                if self._SCHEMA[section][setting]['required'] == True:
                    if section not in config:
                        return False
                    elif setting not in config[section]:
                        return False
                # If setting is not required, check if a default value exists
                #   and set it if not set in the config
                elif 'default' in self._SCHEMA[section][setting]:
                    if section not in config:
                        default_setting = {
                                section: {
                                    setting: self._SCHEMA[section][setting]['default']
                                    }
                                }
                        config.update(default_setting)
                    elif setting not in config[section]:
                        config[section][setting] = self._SCHEMA[section][setting]['default']

        # Write any changes to config back to disk and return True since we found all required settings
        self.set_config(config)
        return True

    @staticmethod
    def list_modes(modes_available):
        modes = [ Mode.Off.name ]
        for mode in modes_available:
            if mode[1] is True:
                modes.append(mode[0])
        if len(modes) == 3:
            modes.append(Mode.Heat_Cool.name)
        return modes