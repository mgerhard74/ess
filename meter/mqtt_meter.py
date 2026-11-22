"""
Grid meter via MQTT (e.g. AMIS reader publishing JSON with "saldo"), same interface as RestMeter

paho-mqtt runs its own network thread with automatic reconnect, the main loop is never blocked.
"""
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt


class MqttMeter:
    def __init__(self, name, host, topic, parse, port=1883, user=None, password=None, lifetime=10):
        """
        :param name:     'mqtt'
        :param host:     broker address
        :param topic:    topic with the meter json, e.g. 'amis/user1'
        :param parse:    function json -> {'p', 'e_import', 'e_export'}, raises on invalid data
        :param lifetime: [s] last valid data is used this long (no new message -> invalid)
        """
        self.name = name
        self.topic = topic
        self.parse = parse
        self.lifetime = lifetime
        self.log = logging.getLogger(name)
        self.data = None
        self.valid_until = 0
        self.lock = threading.Lock()
        self.parse_error_logged = False
        self.connect_error_logged = False

        try:  # paho-mqtt 2.x needs the callback api version, 1.x does not know it
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        except AttributeError:
            self.client = mqtt.Client()
        if user:
            self.client.username_pw_set(user, password)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()  # own thread, reconnects automatically

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.log.info("{} connected, subscribe {}".format(self.name, self.topic))
            self.connect_error_logged = False
            client.subscribe(self.topic)
        elif not self.connect_error_logged:  # 4 = bad user / password, 5 = not authorized, log once
            self.connect_error_logged = True
            self.log.warning("{} connect failed: {}".format(self.name, mqtt.connack_string(rc)))

    def on_disconnect(self, client, userdata, rc):
        if rc != 0 and not self.connect_error_logged:  # only after a successful connect, no repeat on login error
            self.log.warning("{} disconnected (rc={}), reconnecting".format(self.name, rc))

    def on_message(self, client, userdata, msg):
        try:
            d = self.parse(json.loads(msg.payload))
            with self.lock:
                self.data = d
                self.valid_until = time.perf_counter() + self.lifetime
            self.parse_error_logged = False
        except Exception as e:
            if not self.parse_error_logged:  # log once, not with every message
                self.parse_error_logged = True
                self.log.warning("{} invalid message on {}: {} payload={}".format(
                    self.name, msg.topic, e, msg.payload[:200]))

    def get(self):
        """
        :return: dict p / e_import / e_export or None if no valid message within lifetime
        """
        with self.lock:
            return self.data if time.perf_counter() < self.valid_until else None
