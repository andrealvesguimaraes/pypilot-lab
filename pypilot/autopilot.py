#!/usr/bin/env python
#
#   Copyright (C) 2018 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

# autopilot base handles reading from the imu (boatimu)

from __future__ import print_function
import sys, os
import math

pypilot_dir = os.getenv('HOME') + '/.pypilot/'

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import os.path
from server import *
from pipeserver import pypilotPipeServer
from values import *
from boatimu import *
from resolv import *
import tacking, servo
from version import strversion
from sensors import Sensors

def minmax(value, r):
    return min(max(value, -r), r)

def compute_true_wind(gps_speed, wind_speed, wind_direction):
    rd = math.radians(wind_direction)
    windv = wind_speed*math.sin(rd), wind_speed*math.cos(rd)
    truewind = math.degrees(math.atan2(windv[0], windv[1] - gps_speed))
    #print 'truewind', truewind
    return truewind

class Filter(object):
    def __init__(self,filtered, lowpass):
        self.filtered = filtered
        self.lowpass = lowpass
        self.lastfilter_time = time.time()
        
    def update(self, value):
        t = time.time()
        timediff = t - self.lastfilter_time
        self.lastfilter_time = t
        #        rate = self.lowpass.value * timediff
        rate = self.lowpass.value
        if rate > 1:
            rate = 1

        filtered = self.filtered.value
        self.filtered.set(filtered + rate * (value-filtered))
        
class FilterHeading(object):
  def __init__(self, *args, **keywords):
    super(FilterHeading, self).__init__(*args, **keywords)
    self.variables = []

    def update(self, value):
        # detect passing the wrong way around
        if value - self.value > 180:
            self.value = self.value + 360
        elif self.value - value > 180:
            self.value = self.value - 360
        super(FilterHeading, self).update(value)

class ModeProperty(EnumProperty):
      def __init__(self, name):
        self.ap = False
        super(ModeProperty, self).__init__(name, 'compass', ['compass', 'gps', 'wind', 'true wind'], persistent=True)

      def set(self, value):
        # update the preferred mode when the mode changes from user
        if self.ap:
            self.ap.preferred_mode.update(value)
        self.set_internal(value)

      def set_internal(self, value):
        super(ModeProperty, self).set(value)

class HeadingOffset(object):
  def __init__(self):
    self.value = 0

  def update(self, offset, d):
      offset = resolv(offset, self.value)
      self.value = resolv(d*offset + (1-d)*self.value)
  
import pilots
class Autopilot(object):
  def __init__(self):
    super(Autopilot, self).__init__()

    # setup all processes to exit on any signal
    self.childpids = []
    def cleanup(signal_number, frame=None):
        print('got signal', signal_number, 'cleaning up')
        while self.childpids:
            pid = self.childpids.pop()
            #print('kill child', pid)
            os.kill(pid, signal.SIGTERM) # get backtrace
        sys.stdout.flush()
        if signal_number != 'atexit':
            raise KeyboardInterrupt # to get backtrace on all processes

    # unfortunately we occasionally get this signal,
    # some sort of timing issue where python doesn't realize the pipe
    # is broken yet, so doesn't raise an exception
    def printpipewarning(signal_number, frame):
        print('got SIGPIPE, ignoring')

    import signal
    for s in range(1, 16):
        if s == 13:
            signal.signal(s, printpipewarning)
        elif s != 9:
            signal.signal(s, cleanup)

#    self.server = pypilotServer()
    self.server = pypilotPipeServer()
    self.boatimu = BoatIMU(self.server)
    self.sensors = Sensors(self.server)
    self.servo = servo.Servo(self.server, self.sensors)

    self.version = self.Register(Value, 'version', 'pypilot' + ' ' + strversion)
    self.heading_command = self.Register(HeadingProperty, 'heading_command', 0)
    self.enabled = self.Register(BooleanProperty, 'enabled', False)
    self.lastenabled = False

    self.preferred_mode = self.Register(Value, 'preferred_mode', 'compass')
    self.lastmode = False    
    self.mode = self.Register(ModeProperty, 'mode')
    self.mode.ap = self

    self.last_heading = False
    self.last_heading_off = self.boatimu.heading_off.value

    self.pilots = []
    for pilot_type in pilots.default:
        try:
            self.pilots.append(pilot_type(self))
        except Exception as e:
            print('failed to load pilot', pilot_type, e)

    pilot_names = list(map(lambda pilot : pilot.name, self.pilots))
    print('Loaded Pilots:', pilot_names)
    self.pilot = self.Register(EnumProperty, 'pilot', 'basic', pilot_names, persistent=True)

    self.heading = self.Register(SensorValue, 'heading', directional=True)
    self.heading_error = self.Register(SensorValue, 'heading_error')
    self.heading_error_int = self.Register(SensorValue, 'heading_error_int')
    self.heading_error_int_time = time.time()

    self.tack = tacking.Tack(self)

    self.gps_compass_offset = HeadingOffset()
    self.gps_speed = 0

    self.wind_compass_offset = HeadingOffset()
    self.true_wind_compass_offset = HeadingOffset()
    
    self.wind_direction = self.Register(SensorValue, 'wind_direction', directional=True)
    self.wind_speed = 0

    self.runtime = self.Register(TimeValue, 'runtime') #, persistent=True)

    device = '/dev/watchdog0'
    self.watchdog_device = False
    try:
        self.watchdog_device = open(device, 'w')
    except:
        print('warning: failed to open special file', device, 'for writing')
        print('         cannot stroke the watchdog')

    if os.system('sudo chrt -pf 99 %d 2>&1 > /dev/null' % os.getpid()):
        print('warning, failed to make autopilot process realtime')

    self.times = 4*[0]

    self.childpids = [self.boatimu.imu_process.pid, self.boatimu.auto_cal.process.pid,
                      self.sensors.nmea.process.pid, self.sensors.gps.process.pid]
    try:
        self.childpids += [self.server.process.pid]
    except:
        print('warning no server process')
        
    signal.signal(signal.SIGCHLD, cleanup)
    import atexit
    atexit.register(lambda : cleanup('atexit'))
    
    self.lasttime = time.time()

    # read initial value from imu as this takes time
#    while not self.boatimu.IMURead():
#        time.sleep(.1)

  def __del__(self):
      print('closing autopilot')
      self.server.__del__()

      if self.watchdog_device:
          print('close watchdog')
          self.watchdog_device.write('V')
          self.watchdog_device.close()

  def Register(self, _type, name, *args, **kwargs):
    return self.server.Register(_type(*(['ap.' + name] + list(args)), **kwargs))

  def run(self):
      while True:
          self.iteration()

  def adjust_mode(self, pilot):
      # if the mode must change
      newmode = pilot.best_mode(self.preferred_mode.value)
      if self.mode.value != newmode:
          self.mode.set_internal(newmode)

  def compute_offsets(self):
      # compute difference between compass to gps and compass to wind
      compass = self.boatimu.SensorValues['heading_lowpass'].value
      if self.sensors.gps.source.value != 'none':
          d = .002
          gps_speed = self.sensors.gps.speed.value
          self.gps_speed = (1-d)*self.gps_speed + d*gps_speed
          if gps_speed > 1: # don't update gps offset below 1 knot
              gps_track  = self.sensors.gps.track.value
              # weight gps compass offset higher with more gps speed
              d = .005*math.log(self.gps_speed + 1)
              self.gps_compass_offset.update(gps_track - compass, d)

      if self.sensors.wind.source.value != 'none':
          d = .005
          wind_speed = self.sensors.wind.speed.value
          self.wind_speed = (1-d)*self.wind_speed + d*wind_speed
          # weight wind direction more with higher wind speed
          d = .05*math.log(wind_speed/5.0 + 1.2)
          wind_direction = resolv(self.sensors.wind.direction.value, self.wind_direction.value)
          wind_direction = (1-d)*self.wind_direction.value + d*wind_direction
          self.wind_direction.set(resolv(wind_direction, 180))
          self.wind_compass_offset.update(wind_direction + compass, d)

          if self.sensors.gps.source.value != 'none':
            true_wind = compute_true_wind(self.gps_speed, self.wind_speed,
                                          self.wind_direction.value)
            offset = resolv(true_wind + compass, self.true_wind_compass_offset.value)
            d = .05
            self.true_wind_compass_offset.update(offset, d)
    
  def fix_compass_calibration_change(self, data, t0):
      headingrate = self.boatimu.SensorValues['headingrate_lowpass'].value
      dt = t0 - self.lasttime
      self.lasttime = t0
      #if the compass gets a new fix, or the alignment changes,
      # update the autopilot command so the course remains constant
      self.compass_change = 0
      if data:
        if 'compass_calibration_updated' in data and self.last_heading:
          # with compass calibration updates, adjust the compass offset to hold the same course
          # to prevent actual course change
          last_heading = resolv(self.last_heading, data['heading'])
          self.compass_change += data['heading'] - headingrate*dt - last_heading
        self.last_heading = data['heading']

      # if heading offset alignment changed, keep same course
      if self.last_heading_off != self.boatimu.heading_off.value:
          self.last_heading_off = resolv(self.last_heading_off, self.boatimu.heading_off.value)
          self.compass_change += self.boatimu.heading_off.value - self.last_heading_off
          self.last_heading_off = self.boatimu.heading_off.value

      if self.compass_change:
          self.gps_compass_offset.value -= self.compass_change
          self.wind_compass_offset.value += self.compass_change
          self.true_wind_compass_offset.value += self.compass_change
          if self.mode.value == 'compass':
            heading_command = self.heading_command.value + self.compass_change
            self.heading_command.set(resolv(heading_command, 180))
          
  def compute_heading_error(self, t):
      heading = self.heading.value

      # keep same heading if mode changes
      if self.mode.value != self.lastmode:
          error = self.heading_error.value
          if 'wind' in self.mode.value:
              error = -error
          self.heading_command.set(resolv(heading - error, 180))
          self.lastmode = self.mode.value
      
      # compute heading error
      heading_command = self.heading_command.value
      
      # error +- 60 degrees
      err = minmax(resolv(heading - heading_command), 60)
      
      # since wind direction is where the wind is from, the sign is reversed
      if 'wind' in self.mode.value:
          err = -err
      self.heading_error.set(err)

      # compute integral for I gain
      dt = t - self.heading_error_int_time
      dt = max(min(dt, 1), 0) # ensure dt is from 0 to 1
      self.heading_error_int_time = t
      # int error +- 1, from 0 to 1500 deg/s
      self.heading_error_int.set(minmax(self.heading_error_int.value + \
                                        (self.heading_error.value/1500)*dt, 1))
          
  def iteration(self):
      data = False
      t00 = time.time()
      # set timestamp
      for tries in range(14): # try 14 times to read from imu 
          data = self.boatimu.IMURead()
          if data:
              break
          time.sleep(self.boatimu.period/10)

      if not data:
          print('autopilot failed to read imu at time:', time.time())

      t0 = time.time()
      self.fix_compass_calibration_change(data, t0)
      self.compute_offsets()

      pilot = None
      for p in self.pilots:
        if p.name == self.pilot.value or not pilot:
          pilot = p

      self.adjust_mode(pilot)
      pilot.compute_heading()
      self.compute_heading_error(t0)
          
      if self.enabled.value:
          self.runtime.update()
          self.servo.servo_calibration.stop()
      else:
          self.runtime.stop()

      # reset filters when autopilot is enabled
      reset = False
      if self.enabled.value != self.lastenabled:
        self.lastenabled = self.enabled.value
        if self.enabled.value:
          self.heading_error_int.set(0) # reset integral
          reset = True
      
      # perform tacking or pilot specific calculation
      if not self.tack.process():
        pilot.process(reset) # implementation specific process

      # servo can only disengage under manual control
      self.servo.force_engaged = self.enabled.value

      t1 = time.time()
      if t1-t0 > self.boatimu.period/2:
          print('Autopilot routine is running too _slowly_', t1-t0, self.boatimu.period/2)

      self.servo.poll()
      t2 = time.time()
      if t2-t1 > self.boatimu.period/2:
          print('servo is running too _slowly_', t2-t1)

      self.sensors.poll()

      t4 = time.time()
      if t4 - t2 > self.boatimu.period/2:
          print('sensors is running too _slowly_', t4-t2)

      self.server.HandleRequests()
      t5 = time.time()
      if t5 - t4 > self.boatimu.period/2:
          print('server is running too _slowly_', t5-t4)

      times = t1-t0, t2-t1, t4-t2
      #self.times = map(lambda x, y : .975*x + .025*y, self.times, times)
      #print('times', map(lambda t : '%.2f' % (t*1000), self.times))
      
      if self.watchdog_device:
          self.watchdog_device.write('c')

      while True:
          dt = self.boatimu.period - (time.time() - t00)
          if dt <= 0 or dt >= self.boatimu.period:
              break
          time.sleep(dt)


def main():
  ap = Autopilot()
  ap.run()

if __name__ == '__main__':
    main()
