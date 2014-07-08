'''
Copyright (c) 2012-2014, Agora Games, LLC All rights reserved.

https://github.com/agoragames/kairos/blob/master/LICENSE.txt
'''
from .exceptions import *
from .timeseries import *

import operator
import sys
import time
import re
from urlparse import *
from redis import Redis
import redis

class RedisBackend(Timeseries):
  '''
  Redis implementation of timeseries support.
  '''

  def __new__(cls, *args, **kwargs):

    if cls==RedisBackend:
      ttype = kwargs.pop('type', None)
      if ttype=='series':
        return RedisSeries.__new__(RedisSeries, *args, **kwargs)
      elif ttype=='histogram':
        return RedisHistogram.__new__(RedisHistogram, *args, **kwargs)
      elif ttype=='count':
        return RedisCount.__new__(RedisCount, *args, **kwargs)
      elif ttype=='gauge':
        return RedisGauge.__new__(RedisGauge, *args, **kwargs)
      elif ttype=='set':
        return RedisSet.__new__(RedisSet, *args, **kwargs)
      raise NotImplementedError("No implementation for %s types"%(ttype))
    return Timeseries.__new__(cls, *args, **kwargs)

  def __init__(self, client, **kwargs):
    # prefix is redis-only feature (TODO: yes or no?)
    self._prefix = kwargs.get('prefix', '')
    if len(self._prefix) and not self._prefix.endswith(':'):
      self._prefix += ':'

    super(RedisBackend,self).__init__( client, **kwargs )


  @classmethod
  def url_parse(self, url, **kwargs):
    location = urlparse(url)
    if location.scheme == 'redis':
      return Redis.from_url( url, **kwargs )

  def _calc_keys(self, config, name, timestamp):
    '''
    Calculate keys given a stat name and timestamp.
    '''
    i_bucket = config['i_calc'].to_bucket( timestamp )
    r_bucket = config['r_calc'].to_bucket( timestamp )

    i_key = '%s%s:%s:%s'%(self._prefix, name, config['interval'], i_bucket)
    r_key = '%s:%s'%(i_key, r_bucket)

    return i_bucket, r_bucket, i_key, r_key

  def list(self):

    keys = []
    res = self._client.scan()
    while res[0] != '0':
        keys += res[1]
        res = self._client.scan( res[0])
    keys += res[1]
    rval = set()
    for key in keys:
      key = key[len(self._prefix):]
      rval.add( key.split(':')[0] )
    return list(rval)

  def properties(self, name, interval = None, indexer = None ):
    
    prefix = '%s%s:'%(self._prefix,name)

    if interval is not None and self._indexer is not None:
      keys = []
      c = self._indexer.getConnection()
      k = '%s%s'%(prefix,interval)
      results = c.zrange( k, start = 0, end = -1 )
      for r in results:
        keys.append( k + ':' + r )
    else:
      keys = []
      k = '%s*'%(prefix)
      res = self._client.scan( match = k )
      while res[0] != '0':
        if res[1] != []:
          keys += res[1]
        res = self._client.scan( res[0], match = k )
      if res[1] != []:
        keys += res[1]

    rval = {}

    for key in keys:
      key = key[len(prefix):].split(':')
      rval.setdefault( key[0], {} )
      if 'first' in rval[key[0]]:
        rval[key[0]]['first'] = min(rval[key[0]]['first'], int(key[1]))
      else:
        rval[key[0]]['first'] = int(key[1])
      if 'last' in rval[key[0]]:
        rval[key[0]]['last'] = max(rval[key[0]]['last'], int(key[1]))
      else:
        rval[key[0]]['last'] = int(key[1])

    for interval, properties in rval.items():
      # It's possible that there is data stored for which an interval
      # is not defined.
      if interval in self._intervals:
        config = self._intervals[interval]
        properties['first'] = config['i_calc'].from_bucket( properties['first'] )
        properties['last'] = config['i_calc'].from_bucket( properties['last'] )
      else:
        rval.pop(interval)

    return rval

  def _batch_insert(self, inserts, intervals, **kwargs):

    '''
    Specialized batch insert
    '''
    if 'pipeline' in kwargs:
      pipe = kwargs.get('pipeline')
      own_pipe = False
    else:
      pipe = self._client.pipeline(transaction=False)
      kwargs['pipeline'] = pipe
      own_pipe = True

    for timestamp,names in inserts.iteritems():
      for name,values in names.iteritems():
        for value in values:
          # TODO: support config param to flush the pipe every X inserts
          self._insert( name, value, timestamp, intervals, **kwargs )

    if own_pipe:
      kwargs['pipeline'].execute()

  def _insert(self, name, value, timestamp, intervals, **kwargs):
    '''
    Insert the value.
    '''
    if 'pipeline' in kwargs:
      pipe = kwargs.get('pipeline')
    else:
      pipe = self._client.pipeline(transaction=False)


    for interval,config in self._intervals.iteritems():
      timestamps = self._normalize_timestamps(timestamp, intervals, config)
      for tstamp in timestamps:
        self._insert_data(name, value, tstamp, interval, config, pipe, indexer = self._indexer )

    if 'pipeline' not in kwargs:
      pipe.execute()

  def _insert_data(self, name, value, timestamp, interval, config, pipe, indexer = None):
    '''Helper to insert data into redis'''
    # Calculate the TTL and abort if inserting into the past
    expire, ttl = config['expire'], config['ttl'](timestamp)
    if expire and not ttl:
      return

    i_bucket, r_bucket, i_key, r_key = self._calc_keys(config, name, timestamp)


    if indexer is not None:
      indexer.addToIndex( i_key )

    if config['coarse']:
      self._type_insert(pipe, i_key, value)
    else:
      # Add the resolution bucket to the interval. This allows us to easily
      # discover the resolution intervals within the larger interval, and
      # if there is a cap on the number of steps, it will go out of scope
      # along with the rest of the data
      pipe.sadd(i_key, r_bucket)
      self._type_insert(pipe, r_key, value)

    if expire:
      pipe.expire(i_key, ttl)
      if not config['coarse']:
        pipe.expire(r_key, ttl)

  def delete(self, name):
    '''
    Delete all the data in a named timeseries.
    '''
    #keys = self._client.keys('%s%s:*'%(self._prefix,name))
    keys = []
    res = self._client.scan( match = '%s%s:*'%(self._prefix,name))
    while res[0] != '0':
        if res[1] != []:
            keys += res[1]
        res = self._client.scan( res[0], match = '%s%s:*'%(self._prefix,name))
    if res[1] != []:
        keys += res[1]

    pipe = self._client.pipeline(transaction=False)
    for key in keys:
      pipe.delete( key )
    pipe.execute()

    # Could be not technically the exact number of keys deleted, but is a close
    # enough approximation
    return len(keys)

  def _get(self, name, interval, config, timestamp, **kws):
    '''
    Fetch a single interval from redis.
    '''
    i_bucket, r_bucket, i_key, r_key = self._calc_keys(config, name, timestamp)
    fetch = kws.get('fetch') or self._type_get
    process_row = kws.get('process_row') or self._process_row

    rval = OrderedDict()
    if config['coarse']:
      data = process_row( fetch(self._client, i_key) )
      rval[ config['i_calc'].from_bucket(i_bucket) ] = data
    else:
      # First fetch all of the resolution buckets for this set.
      resolution_buckets = sorted(map(int,self._client.smembers(i_key)))

      # Create a pipe and go fetch all the data for each.
      # TODO: turn off transactions here?
      pipe = self._client.pipeline(transaction=False)
      for bucket in resolution_buckets:
        r_key = '%s:%s'%(i_key, bucket)   # TODO: make this the "resolution_bucket" closure?
        fetch(pipe, r_key)
      res = pipe.execute()

      for idx,data in enumerate(res):
        data = process_row(data)
        rval[ config['r_calc'].from_bucket(resolution_buckets[idx]) ] = data

    return rval

  def _series(self, name, interval, config, buckets, **kws):
    '''
    Fetch a series of buckets.
    '''
    pipe = self._client.pipeline(transaction=False)
    step = config['step']
    resolution = config.get('resolution',step)
    fetch = kws.get('fetch') or self._type_get
    process_row = kws.get('process_row') or self._process_row

    rval = OrderedDict()
    for interval_bucket in buckets:
      i_key = '%s%s:%s:%s'%(self._prefix, name, interval, interval_bucket)

      if config['coarse']:
        fetch(pipe, i_key)
      else:
        pipe.smembers(i_key)
    res = pipe.execute()

    # TODO: a memory efficient way to use a single pipeline for this.
    for idx,data in enumerate(res):
      # TODO: use closures on the config for generating this interval key
      interval_bucket = buckets[idx] #start_bucket + idx
      interval_key = '%s%s:%s:%s'%(self._prefix, name, interval, interval_bucket)

      if config['coarse']:
        data = process_row( data )
        rval[ config['i_calc'].from_bucket(interval_bucket) ] = data
      else:
        rval[ config['i_calc'].from_bucket(interval_bucket) ] = OrderedDict()
        pipe = self._client.pipeline(transaction=False)
        resolution_buckets = sorted(map(int,data))
        for bucket in resolution_buckets:
          # TODO: use closures on the config for generating this resolution key
          resolution_key = '%s:%s'%(interval_key, bucket)
          fetch(pipe, resolution_key)

        resolution_res = pipe.execute()
        for x,data in enumerate(resolution_res):
          i_t = config['i_calc'].from_bucket(interval_bucket)
          r_t = config['r_calc'].from_bucket(resolution_buckets[x])
          rval[ i_t ][ r_t ] = process_row(data)

    return rval

class RedisSeries(RedisBackend, Series):

  def _type_insert(self, handle, key, value):
    '''
    Insert the value into the series.
    '''
    handle.rpush(key, value)

  def _type_get(self, handle, key):
    '''
    Get for a series.
    '''
    return handle.lrange(key, 0, -1)

class RedisHistogram(RedisBackend, Histogram):

  def _type_insert(self, handle, key, value):
    '''
    Insert the value into the series.
    '''
    handle.hincrby(key, value, 1)

  def _type_get(self, handle, key):
    return handle.hgetall(key)

class RedisCount(RedisBackend, Count):

  def _type_insert(self, handle, key, value):
    '''
    Insert the value into the series.
    '''
    if value!=0:
      if isinstance(value,float):
        handle.incrbyfloat(key, value)
      else:
        handle.incr(key,value)

  def _type_get(self, handle, key):
    return handle.get(key)

class RedisGauge(RedisBackend, Gauge):

  def _type_insert(self, handle, key, value):
    '''
    Insert the value into the series.
    '''
    handle.set(key, value)

  def _type_get(self, handle, key):
    return handle.get(key)

class RedisSet(RedisBackend, Set):

  def _type_insert(self, handle, key, value):
    '''
    Insert the value into the series.
    '''
    handle.sadd(key, value)

  def _type_get(self, handle, key):
    return handle.smembers(key)
