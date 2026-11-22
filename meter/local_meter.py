"""
Smart meter and PV: grid meter(s) and Fronius PV inverter are read directly, each device in its own thread.
read() gives a dictionary with grid_p, home_p, home_all_p, pv_p, ... for regulation, web, trace and blackbox.

    grid meter  'amis'   AMIS reader (IR reading head) via REST   http://<ip>/rest
                'shelly' Shelly Pro 3EM                           http://<ip>/rpc/Shelly.GetStatus
                'mqtt'   AMIS reader via MQTT (JSON with "saldo")
    PV          Fronius Symo / Gen24 Solar API                    (optional)
"""
import logging
import time
from datetime import datetime

from meter.fronius import Symo
from meter.rest_meter import RestMeter, parse_amis, parse_shelly_pro3em


class LocalMeter:
    def __init__(self, cfg, secret=None, log_name='meter'):
        """
        :param cfg:    config['meter'] dictionary
        :param secret: config_secret dictionary (mqtt_user, mqtt_password)
        """
        self.log = logging.getLogger(log_name)
        self.data = None
        self.grid_source = None
        secret = secret or {}
        lifetime = cfg.get('lifetime', 10)  # [s] last valid data is used this long after a read error
        interval = cfg.get('grid_meter_interval', 0.25)

        self.grid_meter = []  # priority list, the following meters are the backup
        for name in cfg.get('grid_meter', ['amis']):
            if name == 'amis':
                url = 'http://{}/rest'.format(cfg.get('amis_address', ''))
                self.grid_meter.append(RestMeter('amis', url, parse_amis, lifetime=lifetime, interval=interval))
            elif name == 'shelly':
                url = 'http://{}/rpc/Shelly.GetStatus'.format(cfg.get('shelly_3em_address', ''))
                invert = cfg.get('shelly_3em_invert', False)
                self.grid_meter.append(RestMeter('shelly', url, lambda d, inv=invert: parse_shelly_pro3em(d, inv),
                                                 lifetime=lifetime, interval=interval))
            elif name == 'mqtt':
                from meter.mqtt_meter import MqttMeter  # paho-mqtt only needed with mqtt
                self.grid_meter.append(MqttMeter('mqtt', cfg.get('mqtt_address', ''), cfg.get('mqtt_topic', ''),
                                                 parse_amis, port=cfg.get('mqtt_port', 1883),
                                                 user=secret.get('mqtt_user'), password=secret.get('mqtt_password'),
                                                 lifetime=lifetime))
            else:
                self.log.error("unknown grid_meter '{}' in config.py".format(name))
                continue
            self.log.info("grid meter {}".format(name))

        self.pv = None
        if cfg.get('fronius_address'):
            self.pv = Symo(cfg['fronius_address'], timeout=5, lifetime=lifetime, log_name='fronius')
            self.pv.start_tread(thread_sleep=cfg.get('fronius_interval', 1.0))

    def read(self):
        """
        Collect the newest values of all devices (no I/O here, devices are polled in their own threads).

        :return: data dictionary
        """
        data = {'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'timestamp': int(time.time())}

        # grid meter: first meter with valid data, all invalid -> None (ESS goes idle)
        grid = None
        source = None
        for m in self.grid_meter:
            d = m.get()
            data['grid_p_' + m.name] = None if d is None else d['p']  # each meter, for comparison
            if grid is None and d is not None:
                grid, source = d, m.name
        if source != self.grid_source:
            self.log.warning("grid meter source: {} -> {}".format(self.grid_source, source))
            self.grid_source = source
        saldo = None if grid is None else grid['p']
        data['grid_source'] = source
        data['grid_imp_eto'] = None if grid is None else grid['e_import']
        data['grid_exp_eto'] = None if grid is None else grid['e_export']
        data['grid_p'] = saldo
        data['home_p'] = saldo  # smart meter saldo = house consumption the battery has to cover
        data['home_all_p'] = saldo

        # PV (sum of all inverters of the Fronius system), 0 W if not configured or not reachable
        pv = self.pv.data if self.pv else None  # None: not configured or not reachable (lifetime expired)
        total = lambda key: sum(v for v in pv.get(key, []) if v is not None) if pv else None
        data['pv_p'] = total('p') or 0
        data['pv_e_day'] = total('e_day')
        data['pv_eto'] = total('e_total')

        data['bat_p'] = 0  # no battery meter (battery power from multiplus)
        data['car_p'] = None  # no wallbox

        self.data = data
        return data
