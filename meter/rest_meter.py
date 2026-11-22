"""
Grid meter via HTTP REST, polled in an own thread (a slow or offline device never blocks the main loop)

    AMIS reader (IR reading head)   http://<ip>/rest
    Shelly Pro 3EM (Gen2 RPC)       http://<ip>/rpc/Shelly.GetStatus

Values: p [W] (+ grid import, - grid export), e_import / e_export [Wh]
"""
import logging
import threading
import time

import requests


def parse_amis(d):
    """
    {"1.8.0": 17191430, "2.8.0": 30515406, "1.7.0": 830, "2.7.0": 0, "saldo": 830, ...}
    """
    return {'p': float(d['saldo']), 'e_import': d.get('1.8.0'), 'e_export': d.get('2.8.0')}


def parse_shelly_pro3em(d, invert=False):
    """
    Shelly.GetStatus, triphase profile:  "em:0": {"total_act_power": 830.5, ...}, "emdata:0": {"total_act": .., "total_act_ret": ..}
                      monophase profile: "em1:0".."em1:2": {"act_power": ..}, "em1data:0".."em1data:2": {"total_act_energy": .., "total_act_ret_energy": ..}
    """
    if 'em:0' in d:
        p = float(d['em:0']['total_act_power'])
        e = d.get('emdata:0') or {}
        e_import, e_export = e.get('total_act'), e.get('total_act_ret')
    else:
        phases = [k for k in ('em1:0', 'em1:1', 'em1:2') if k in d]
        if not phases:
            raise ValueError('no em:0 / em1:x in shelly status')
        p = sum(float(d[k]['act_power']) for k in phases)
        try:
            e_import = sum(d['em1data:' + k[-1]]['total_act_energy'] for k in phases)
            e_export = sum(d['em1data:' + k[-1]]['total_act_ret_energy'] for k in phases)
        except Exception:
            e_import, e_export = None, None
    if invert:  # current clamps mounted in the other direction
        p = -p
        e_import, e_export = e_export, e_import
    r = lambda v: None if v is None else round(v)
    return {'p': round(p), 'e_import': r(e_import), 'e_export': r(e_export)}


class RestMeter:
    def __init__(self, name, url, parse, lifetime=10, interval=1.0, timeout=1.0):
        """
        :param name:     'amis', 'shelly'
        :param url:      full url
        :param parse:    function json -> {'p', 'e_import', 'e_export'}, raises on invalid data
        :param lifetime: [s] last valid data is used this long after a read error
        :param interval: [s] poll interval
        :param timeout:  [s] http timeout
        """
        self.name = name
        self.url = url
        self.parse = parse
        self.lifetime = lifetime
        self.interval = interval
        self.timeout = timeout
        self.log = logging.getLogger(name)
        self.data = None
        self.valid_until = 0
        self.online = True
        self.lock = threading.Lock()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            t0 = time.perf_counter()
            try:
                r = requests.get(self.url, allow_redirects=False, timeout=self.timeout)
                r.raise_for_status()
                d = self.parse(r.json())
                with self.lock:
                    self.data = d
                    self.valid_until = time.perf_counter() + self.lifetime
                if not self.online:
                    self.online = True
                    self.log.info("{} reachable again".format(self.name))
            except Exception as e:
                if self.online:
                    self.online = False
                    self.log.warning("{} not reachable: {}".format(self.name, e))
            time.sleep(max(0.1, self.interval - (time.perf_counter() - t0)))

    def get(self):
        """
        :return: dict p / e_import / e_export or None if no valid data within lifetime
        """
        with self.lock:
            return self.data if time.perf_counter() < self.valid_until else None
