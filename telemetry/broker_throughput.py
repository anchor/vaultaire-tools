#!/usr/bin/env python 

'''use burstnetsink to tap a broker's debug stream and output throughput info

e.g.:

    $ burstnetsink -v -p -b tcp://broker:5000 2>&1 | broker_throughput.py
'''

import sys
from time import *
import os
import fcntl

class TimeAware(object):
    '''simple timing aware mixin

    The default implementation of on_tick_change() is to call every function
    passed to the constructor in tick_handlers
    '''
    def __init__(self, ticklen=1, tick_handlers=[]):
        self.last_tick = self.start_time = time()
        self.ticklen = ticklen
        self.tick_handlers = tick_handlers
        self.n_ticks = 0
        self.totalticktime = 0
    def check_for_tick_changed(self):
        '''run on_tick_change once for every ticklen seconds that has passed since last_tick
        '''
        tnow = time()
        while tnow - self.last_tick >= self.ticklen:
            self.n_ticks += 1
            self.totalticktime += self.ticklen
            self.last_tick += self.ticklen
            self.on_tick_change()
    def on_tick_change(self):
        '''handler for a tick change
        the timestamp marking the 'tick' being handled is in self.last_tick
        The current time may however be significantly after self.last_tick if
        check_for_tick_changed is not called more often than self.ticklen
        '''
        for f in self.tick_handlers: f()
    def run_forever(self,sleep_time=None):
        '''run in a loop regularly calling on_tick_change
        '''
        if sleep_time == None: sleep_time = self.ticklen/10.0
        while True:
            self.check_for_tick_changed()
            sleep(sleep_time)

class TimeHistogram(TimeAware):
    '''implements a rolling histogram'''
    def __init__(self, nbins, seconds_per_bin=1):
        TimeAware.__init__(self, seconds_per_bin)
        self.nbins = nbins
        self._bins = [0 for n in range(nbins)]
        self.current_bin = 0
    def on_tick_change(self):
        self.current_bin = (self.current_bin + 1) % self.nbins
        self._bins[self.current_bin] = 0
    def add(self, n=1):
        '''add 'n' to the current histogram bin
        '''
        self.check_for_tick_changed()
        self._bins[self.current_bin] += n
    def sum(self, k=60):
        '''return the total entries per second over the last k seconds
        '''
        bins_to_check = k/self.ticklen
        return sum(self.bins[-bins_to_check:])
    def mean(self, k=60):
        '''return the mean entries per second over the last k seconds
        '''
        if self.totalticktime < k:
            k = self.totalticktime  # Only average over the time we've been running
        bins_to_check = k/self.ticklen
        return self.sum(k) / float(bins_to_check) if bins_to_check else 0
    @property 
    def bins(self):
        '''get bins in time order, oldest to newest'''
        self.check_for_tick_changed()
        return self._bins[self.current_bin+1:]+self._bins[:self.current_bin+1]

class ThroughputCounter(object):
    def __init__(self, input_stream=sys.stdin):
        self.input_stream=input_stream

        self.point_hist = TimeHistogram(600) 
        self.burst_hist = TimeHistogram(600) 
        self.acked_burst_hist = TimeHistogram(600) 
        self.latency_hist = TimeHistogram(600) 
        self.ack_hist = TimeHistogram(600) 
        self.outstanding_bursts = {}  # burstid -> start timestamp,points
        self._reader_state = {}

    def get_outstanding(self,last_n_seconds=[10,60]):
        total_burst_counts = map(self.point_hist.sum, last_n_seconds)
        total_ack_counts = map(self.ack_hist.sum, last_n_seconds)
        return [nbursts-nacks for nbursts,nacks in zip(total_burst_counts,total_ack_counts)]
    def get_total_outstanding_points(self):
        return sum(points for timestamp,points in self.outstanding_bursts.itervalues())
    def get_points_per_seconds(self,over_seconds=[600,60,1]):
        return map(self.point_hist.mean, over_seconds)
    def get_total_bursts(self,over_seconds=[600,60,1]):
        return map(self.burst_hist.mean, over_seconds)
    def get_acks_per_second(self,over_seconds=[600,60,1]):
        return map(self.ack_hist.mean, over_seconds)
    def get_average_latencies(self,over_seconds=[600,60,1]):
        burst_counts = map(self.acked_burst_hist.sum, over_seconds)
        latency_sums = map(self.latency_hist.sum, over_seconds)
        return [latencysum/float(nbursts) if nbursts > 0 else 0 for latencysum,nbursts in zip(latency_sums,burst_counts)]

    def process_burst(self, data):
        if not all(k in data for k in ('identity','message id','points')): 
            print >> sys.stderr, 'malformed databurst info. ignoring'
            return
        msgtag = data['identity']+data['message id']
        points = int(data['points'])
        timestamp = time()
        self.outstanding_bursts[msgtag] = timestamp,points
        self.burst_hist.add(1)
        self.point_hist.add(points)

    def process_ack(self, data):
        if not all(k in data for k in ('identity','message id')): 
            print >> sys.stderr, 'malformed ack info. ignoring'
            return

        msgtag = data['identity']+data['message id']
        if msgtag not in self.outstanding_bursts:
            # got ack we didn't see the burst for. ignoring it.
            return

        burst_timestamp,points = self.outstanding_bursts.pop(msgtag)
        latency = time() - burst_timestamp
        self.ack_hist.add(points)
        self.acked_burst_hist.add(1)
        self.latency_hist.add(latency)

    def process_line(self, line):
        '''process a line of burstnetsink trace output

        sample:
            got ingestd ACK
                identity:   0x00e43c9880
                message id: 0xeb9a
            received 5222 bytes
                identity:   0x00e43c9877
                message id: 0xf394
                compressed: 5214 bytes
                uncompressed:       61133 bytes
                points:             405
        '''
        line = line.strip()
        state = self._reader_state
        k,tok,v = line.partition(':')
        if tok == ':':
            state[k] = v.strip()
            if state.get('reading') == 'burst' and k == 'points':
                self.process_burst(state)
                self_reader_state = state = {}
            if state.get('reading') == 'ack' and k == 'message id':
                self.process_ack(state)
                self_reader_state = state = {}
        if 'received ' in line: 
            self._reader_state = {'reading':'burst', 'bytes': int(line.split()[1])}
        elif 'got ingestd ACK' in line:
            self._reader_state = {'reading':'ack'}

    def process_lines_from_stream(self):
        '''process any lines from our streams that are available to read'''
        while True:
            try:
                l = self.input_stream.readline()
                self.process_line(l)
            except IOError:
                # Nothing left to read at the moment
                return


class ThroughputPrinter(object):
    def __init__(self, counter, outstream=sys.stdout, avgtimes=(600,60,1)):
        self.counter = counter
        self.outstream = outstream
        self.avgtimes = avgtimes
        self.lines_printed = 0

    def print_header(self):
        colbreak = " " * 3
        header = '#'
        header += "mean points per second".center(29) + colbreak
        header += "mean acks per second".center(30) + colbreak
        header += "mean latency per point".center(30) + colbreak
        header += "unacked".rjust(10) + '\n'

        header += "#"
        header += "".join(("(%dsec)" % secs).rjust(10) for secs in self.avgtimes)[1:]
        header += colbreak
        header += "".join(("(%dsec)" % secs).rjust(10) for secs in self.avgtimes)
        header += colbreak
        header += "".join(("(%dsec)" % secs).rjust(10) for secs in self.avgtimes)
        header += colbreak + "points".rjust(10) + '\n'

        header += '# ' + '-'*28 + colbreak + '-'*30 + colbreak + '-'*30 
        header += colbreak + '-'*10 + '\n'

        self.outstream.write(header)
        self.outstream.flush()

    def print_throughput(self):
        bursts_per_second = self.counter.get_points_per_seconds(self.avgtimes)
        acks_per_second = self.counter.get_acks_per_second(self.avgtimes)
        mean_latencies = self.counter.get_average_latencies(self.avgtimes)
        outstanding_points = self.counter.get_total_outstanding_points()

        # RENDER ALL THE THINGS!
        out = ""

        colbreak = " " * 3
        out += "".join((" %9.2f" % b for b in bursts_per_second))
        out += colbreak
        out += "".join((" %9.2f" % b for b in acks_per_second))
        out += colbreak
        out += "".join((" %9.2f" % b for b in mean_latencies))
        out += colbreak
        out += "%10d" % outstanding_points + '\n'

        if self.lines_printed % 20 == 0:
            self.print_header()

        self.outstream.write(out)
        self.outstream.flush()
        self.lines_printed += 1


if __name__ == '__main__':

    # Make stdin non-blocking
    fd = sys.stdin.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    reader = ThroughputCounter(sys.stdin)
    writer = ThroughputPrinter(reader, sys.stdout)
    
    # Run an event loop to process outstanding input every second
    # and then output the processed data

    event_loop = TimeAware(1, [ reader.process_lines_from_stream,
                                writer.print_throughput ])
    event_loop.run_forever()

