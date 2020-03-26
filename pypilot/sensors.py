#!/usr/bin/env python
#
#   Copyright (C) 2019 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

from __future__ import print_function
from pypilot.server import *
from pypilot.values import *
from pypilot.resolv import resolv

# favor lower priority sources
source_priority = {'gpsd' : 1, 'servo': 1, 'serial' : 2, 'tcp' : 3, 'tcp-pypilot' : 4, 'none' : 5}

class Sensor(object):
    def __init__(self, server, name):
        self.source = server.Register(StringValue(name + '.source', 'none'))
        self.lastupdate = 0
        self.device = None
        self.name = name
        self.server = server
            
    def write(self, data, source):
        if source_priority[self.source.value] < source_priority[source]:
            return False               

        # if there are more than one device for a source at the same priority,
        # we only use data from one rather than randomly switching between the two
        if source_priority[self.source.value] == source_priority[source] and \
           data['device'] != self.device:
            return False

        #timestamp = data['timestamp'] if 'timestamp' in data else time.time()-self.starttime
        self.update(data)
                
        if self.source.value != source:
            print('found', self.name, 'on', source, data['device'])
            self.source.set(source)
            self.device = data['device']
        self.lastupdate = time.time()
        
        return True

    def reset(self):
        raise 'reset should be overloaded'

    def update(self, data):
        raise 'update should be overloaded'

    def Register(self, _type, name, *args, **kwargs):
        return self.server.Register(_type(*([self.name + '.' + name] + list(args)), **kwargs))

class Wind(Sensor):
    def __init__(self, server):
        super(Wind, self).__init__(server, 'wind')

        self.direction = self.Register(SensorValue, 'direction', directional=True)
        self.speed = self.Register(SensorValue, 'speed')
        self.offset = self.Register(RangeSetting, 'offset', 0, -180, 180, 'deg')

    def update(self, data):
        self.direction.set(resolv(data['direction'] + self.offset.value, 180))
        if 'speed' in data:
            self.speed.set(data['speed'])

    def reset(self):
        self.direction.set(False)
        self.speed.set(False)

class APB(Sensor):
    def __init__(self, server):
        super(APB, self).__init__(server, 'apb')
        self.track = self.Register(SensorValue, 'track', directional=True)
        self.xte = self.Register(SensorValue, 'xte')
        # 300 is 30 degrees for 1/10th mile
        self.gain = self.Register(RangeProperty, 'xte.gain', 300, 0, 3000, persistent=True)
        self.last_time = time.time()

    def reset(self):
        self.xte.update(0)

    def update(self, data):
        t = time.time()
        if t - self.last_time < .5: # only accept apb update at 2hz
            return

        self.last_time = t
        self.track.update(data['track'])
        self.xte.update(data['xte'])

        if not 'ap.enabled' in self.server.values:
            print('ERROR, parsing apb without autopilot')
            return

        if not self.server.values['ap.enabled'].value:
            return

        mode = self.server.values['ap.mode']
        if mode.value != data['mode']:
            # for GPAPB, ignore message on wrong mode
            if data['**'] == 'GP':
                return
            mode.set(data['mode'])

        command = data['track'] + self.gain.value*data['xte']

        heading_command = self.server.values['ap.heading_command']
        if abs(heading_command.value - command) > .1:
            heading_command.set(command)

    
class Sensors(object):
    def __init__(self, server):
        from gpsd import gpsd
        from rudder import Rudder
        from nmea import Nmea
        
        self.server = server
        self.nmea = Nmea(server, self)
        self.gps = gpsd(server, self)
        self.wind = Wind(server)
        self.rudder = Rudder(server)
        self.apb = APB(server)

        self.sensors = {'gps': self.gps, 'wind': self.wind, 'rudder': self.rudder, 'apb': self.apb}

    def poll(self):
        self.gps.poll()
        self.nmea.poll()
        self.rudder.poll()

        # timeout sources
        t = time.time()
        for name in self.sensors:
            sensor = self.sensors[name]
            if sensor.source.value == 'none':
                continue
            if t - sensor.lastupdate > 8:
                self.lostsensor(sensor)

    def lostsensor(self, sensor):
        print('sensor', sensor.name, 'lost', sensor.source.value, sensor.device)
        sensor.source.set('none')
        sensor.reset()
        sensor.device = None
            
    def write(self, sensor, data, source):
        if not sensor in self.sensors:
            print('unknown data parsed!', sensor)
            return
        self.sensors[sensor].write(data, source)

    def lostdevice(self, device):
        # optional routine  useful when a device is
        # unplugged to skip the normal data timeout
        for name in self.sensors:
            sensor = self.sensors[name]
            if sensor.device and sensor.device[2:] == device:
                self.lostsensor(sensor)


if __name__ == '__main__':
    if os.system('sudo chrt -pf 1 %d 2>&1 > /dev/null' % os.getpid()):
      print('warning, failed to make sensor process realtime')
    server = pypilotServer()
    sensors = Sensors(server)

    while True:
        sensors.poll()
        server.HandleRequests()
        time.sleep(.1)
