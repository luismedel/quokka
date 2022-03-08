import numpy as np
import pandas as pd
import ray
from collections import deque, OrderedDict
from dataset import InputCSVDataset, InputMultiParquetDataset, InputSingleParquetDataset, RedisObjectsDataset
import pickle
import os
import redis
from threading import Lock
import time
import boto3
import gc
import sys
# isolated simplified test bench for different fault tolerance protocols

ray.init(ignore_reinit_error=True)


NUM_MAPPERS = 2
NUM_JOINS = 4

class Node:

    # will be overridden
    def __init__(self, id, channel) -> None:
        self.id = id
        self.channel = channel
        self.targets = {}
        self.r = redis.Redis(host='localhost', port=6800, db=0)
        self.target_rs = {}
        self.target_ps = {}

        # track the targets that are still alive
        self.alive_targets = {}
        self.output_lock = Lock()


    def append_to_targets(self,tup):
        node_id, channel_to_ip, partition_key = tup

        unique_ips = set(channel_to_ip.values())
        redis_clients = {i: redis.Redis(host=i, port=6800, db=0) if i != ray.util.get_node_ip_address() else redis.Redis(host='localhost', port = 6800, db=0) for i in unique_ips}
        self.targets[node_id] = (channel_to_ip, partition_key)
        self.target_rs[node_id] = {}
        self.target_ps[node_id] = {}

        for channel in channel_to_ip:
            self.target_rs[node_id][channel] = redis_clients[channel_to_ip[channel]]
        
        for client in redis_clients:
            pubsub = redis_clients[client].pubsub(ignore_subscribe_messages = True)
            pubsub.subscribe("node-done-"+str(node_id))
            self.target_ps[node_id][channel] = pubsub
        
        self.alive_targets[node_id] = {i for i in channel_to_ip}
        # remember the self.strikes stuff? Now we cannot check for that because a downstream target could just die.
        # it's ok if we send stuff to dead people. Fault tolerance is supposed to take care of this.
        
        self.target_output_state[node_id] = {channel:0 for channel in channel_to_ip}

    # meant to be called as a remote method during fault recovery
    def update_target_ip(self, node_id, channel, new_ip):
        redis_client = redis.Redis(host=new_ip, port = 6800, db=0) 
        pubsub = redis_client.pubsub(ignore_subscribe_messages = True)
        pubsub.subscribe("node-done-"+str(node_id))
        self.target_rs[node_id][channel] = redis_client
        self.target_ps[node_id][channel] = pubsub

    def update_target_ip_nd_help_target_recover(self, target_id, channel, target_out_seq_state, new_ip):

        assert new_ip != self.targets[target_id][0][channel] # shouldn't schedule to same IP address ..

        redis_client = redis.Redis(host=new_ip, port = 6800, db=0) 
        pubsub = redis_client.pubsub(ignore_subscribe_messages = True)
        pubsub.subscribe("node-done-"+str(target_id))
        self.target_rs[target_id][channel] = redis_client
        self.target_ps[target_id][channel] = pubsub

        # send logged outputs 
        print("HELP RECOVER",target_id,channel, target_out_seq_state)
        self.output_lock.acquire()
        pipeline = self.target_rs[target_id][channel].pipeline()
        for key in self.logged_outputs:
            if key > target_out_seq_state:
                print("RESENDING", key)
                pipeline.publish("mailbox-"+str(target_id) + "-" + str(channel),pickle.dumps(self.targets[target_id][1](self.logged_outputs[key],channel)))
                pipeline.publish("mailbox-id-"+str(target_id) + "-" + str(channel),pickle.dumps((self.id, self.channel, key)))
        results = pipeline.execute()
        if False in results:
            raise Exception
        self.output_lock.release()

    def truncate_logged_outputs(self, target_id, channel, target_ckpt_state):
        
        print("STARTING TRUNCATE", target_id, channel, target_ckpt_state, self.target_output_state)
        old_min = min(self.target_output_state[target_id].values())
        self.target_output_state[target_id][channel] = target_ckpt_state
        new_min = min(self.target_output_state[target_id].values())

        self.output_lock.acquire()
        if new_min > old_min:
            for key in range(old_min, new_min):
                if key in self.logged_outputs:
                    print("REMOVING KEY",key,"FROM LOGGED OUTPUTS")
                    self.logged_outputs.pop(key)
        self.output_lock.release()    

    def update_targets(self):

        for target_node in self.target_ps:
            # there are #-ip locations you need to poll here.
            for channel in self.target_ps[target_node]:
                client = self.target_ps[target_node][channel]
                while True:
                    message = client.get_message()
                    
                    if message is not None:
                        print(message['data'])
                        self.alive_targets[target_node].remove(int(message['data']))
                        if len(self.alive_targets[target_node]) == 0:
                            self.alive_targets.pop(target_node)
                    else:
                        break 
        if len(self.alive_targets) > 0:
            return True
        else:
            return False

    # reliably log state tag
    def log_state_tag(self):
        assert self.r.rpush("state-tag-" + str(self.id), pickle.dumps(self.state_tag))

    def push(self, data):
            
        print("stream psuh start",time.time())

        self.out_seq += 1

        self.output_lock.acquire()
        self.logged_outputs[self.out_seq] = data
        self.output_lock.release()

        # downstream targets are done. You should be done too then.
        if not self.update_targets():
            print("stream psuh end",time.time())
            return False

        if type(data) == pd.core.frame.DataFrame:
            for target in self.alive_targets:
                original_channel_to_ip, partition_key = self.targets[target]
                for channel in self.alive_targets[target]:
                    if partition_key is not None:

                        if type(partition_key) == str:
                            payload = data[data[partition_key] % len(original_channel_to_ip) == channel]
                            print("payload size ",payload.memory_usage().sum(), channel)
                        elif callable(partition_key):
                            payload = partition_key(data, channel)
                        else:
                            raise Exception("Can't understand partition strategy")
                    else:
                        payload = data
                    # don't worry about target being full for now.
                    print("not checking if target is full. This will break with larger joins for sure.")
                    pipeline = self.target_rs[target][channel].pipeline()
                    pipeline.publish("mailbox-"+str(target) + "-" + str(channel),pickle.dumps(payload))

                    pipeline.publish("mailbox-id-"+str(target) + "-" + str(channel),pickle.dumps(self.id, self.channel, self.out_seq))
                    results = pipeline.execute()
                    if False in results:
                        print("Downstream failure detected")
        else:
            raise Exception

        print("stream psuh end",time.time())
        return True

    def done(self):

        self.out_seq += 1

        print("IM DONE", self.id)

        self.output_lock.acquire()
        self.logged_outputs[self.out_seq] = "done"
        self.output_lock.release()

        if not self.update_targets():
            return False

        for target in self.alive_targets:
            for channel in self.alive_targets[target]:
                pipeline = self.target_rs[target][channel].pipeline()
                pipeline.publish("mailbox-"+str(target) + "-" + str(channel),pickle.dumps("done"))
                pipeline.publish("mailbox-id-"+str(target) + "-" + str(channel),pickle.dumps((self.id, self.channel, self.out_seq)))
                results = pipeline.execute()
                if False in results:
                    print("Downstream failure detected")
        return True

@ray.remote
class InputNode(Node):
    def __init__(self, id, channel, batch_func = None, dependent_map = {}, ckpt = None) -> None:

        super().__init__( id, channel) 

        # track the targets that are still alive
        print("INPUT ACTOR LAUNCH", self.id)

        self.batch_func = batch_func
        self.dependent_rs = {}
        self.dependent_parallelism = {}
        for key in dependent_map:
            self.dependent_parallelism[key] = dependent_map[key][1]
            ps = []
            for ip in dependent_map[key][0]:
                r = redis.Redis(host=ip, port=6800, db=0)
                p = r.pubsub(ignore_subscribe_messages=True)
                p.subscribe("input-done-" + str(key))
                ps.append(p)
            self.dependent_rs[key] = ps

        if ckpt is None:
            self.logged_outputs = OrderedDict()
            self.target_output_state = {}
            self.out_seq = 0
            self.state_tag = 0
            self.state = None
        else:
            recovered_state = pickle.load(open(ckpt,"rb"))
            self.logged_outputs = recovered_state["logged_outputs"]
            self.target_output_state = recovered_state["target_output_state"]
            self.state = recovered_state["state"]
            self.out_seq = recovered_state["out_seq"]
            self.state_tag = recovered_state["tag"]

        
    def checkpoint(self):

        # write logged outputs, state, state_tag to reliable storage
        # for input nodes, log the outputs instead of redownlaoding is probably worth it. since the outputs could be filtered by predicate
        state = { "logged_outputs": self.logged_outputs, "out_seq" : self.out_seq, "tag":self.state_tag, "target_output_state":self.target_output_state,
        "state":self.state}
        pickle.dump(state, open("ckpt-" + str(self.id) + "-temp.pkl","wb"))

        # if this fails we are dead, but probability of this failing much smaller than dump failing
        os.rename("ckpt-" + str(self.id) + "-temp.pkl", "ckpt-" + str(self.id) + ".pkl")
        
    def execute(self):
        
        undone_dependencies = len(self.dependent_rs)
        while undone_dependencies > 0:
            time.sleep(0.001) # be nice
            for dependent_node in self.dependent_rs:
                messages = [i.get_message() for i in self.dependent_rs[dependent_node]]
                for message in messages:
                    if message is not None:
                        if message['data'].decode("utf-8") == "done":
                            self.dependent_parallelism[dependent_node] -= 1
                            if self.dependent_parallelism[dependent_node] == 0:
                                undone_dependencies -= 1
                        else:
                            raise Exception(message['data'])            

        # no need to log the state tag in an input node since we know the expected path...

        for pos, batch in self.input_generator:
            self.state = pos
            if self.batch_func is not None:
                result = self.batch_func(batch)
                self.push(result)
            else:
                self.push(batch)
            if self.state_tag % 10 == 0:
                self.checkpoint()
            self.state_tag += 1
        
        self.done()
        self.r.publish("input-done-" + str(self.id), "done")
    
@ray.remote
class InputCSVNode(InputNode):
    def __init__(self, id, channel, bucket, key, names, num_channels, batch_func = None, dependent_map = {}, ckpt = None) -> None:

        super().__init__(id, channel, batch_func = batch_func, dependent_map = dependent_map, ckpt = ckpt)
        self.accessor = InputCSVDataset(bucket, key, names, 0, stride = 1024 * 1024)
        self.accessor.set_num_mappers(num_channels)
        self.input_generator = self.accessor.get_next_batch(channel, self.state)    

@ray.remote
class InputS3MultiParquetNode(InputNode):

    def __init__(self, id, channel, bucket, key, num_channels, columns = None, batch_func=None, dependent_map={}, ckpt = None):
        
        super().__init__(id, channel, batch_func = batch_func, dependent_map = dependent_map, ckpt = ckpt)
        self.accessor = InputMultiParquetDataset(bucket, key, columns = columns)
        self.accessor.set_num_mappers(num_channels)
        self.input_generator = self.accessor.get_next_batch(channel, self.state)

@ray.remote
class InputRedisDatasetNode(InputNode):
    def __init__(self, id, channel,channel_objects, batch_func=None, dependent_map={}, ckpt = None):
        super().__init__(id, channel, batch_func = batch_func, dependent_map = dependent_map, ckpt = ckpt)
        ip_set = set()
        for channel in self.channel_objects:
            for object in self.channel_objects[channel]:
                ip_set.add(object[0])
        self.accessor = RedisObjectsDataset(channel_objects, ip_set)
        self.input_generator = self.accessor.get_next_batch(channel, self.state)


class TaskNode(Node):
    def __init__(self, id, channel,  mapping, datasets, functionObject, parents, checkpoint_location, checkpoint_interval = 10, ckpt = None) -> None:

        # id: int. Id of the node
        # channel: int. Channel of the node
        # streams: dictionary of logical_id : streams
        # mapping: the mapping between the name you assigned the stream to the actual id of the string.

        super().__init__(id, channel)

        self.p = self.r.pubsub(ignore_subscribe_messages=True)
        self.p.subscribe("mailbox-" + str(id), "mailbox-id-" + str(id))
        self.buffered_inputs = {(parent, channel): deque() for parent in parents for channel in parents[parent]}
        self.id = id 
        self.parents = parents # dict of id -> dict of channel -> actor handles        
        self.checkpoint_location = checkpoint_location
        self.functionObject = functionObject
        self.datasets = datasets
        self.physical_to_logical_stream_mapping = mapping
        self.checkpoint_interval = checkpoint_interval

        if ckpt is None:
            self.state_tag =  {(parent,channel): 0 for parent in parents for channel in parents[parent]}
            self.latest_input_received = {(parent,channel): 0 for parent in parents for channel in parents[parent]}
            self.logged_outputs = OrderedDict()
            self.target_output_state = {}

            self.out_seq = 0
            self.expected_path = deque()

            self.ckpt_counter = -1

        else:

            bucket, key = checkpoint_location
            s3_resource = boto3.resource('s3')
            body = s3_resource.Object(bucket, key).get()['Body']
            recovered_state = pickle.loads(body.read())

            self.state_tag= recovered_state["tag"]
            print("RECOVERED TO STATE TAG", self.state_tag)
            self.latest_input_received = recovered_state["latest_input_received"]
            self.functionObject.deserialize(self.recovered_state["function_object"])
            self.out_seq = recovered_state["out_seq"]
            self.logged_outputs = recovered_state["logged_outputs"]
            self.target_output_state = recovered_state["target_output_state"]

            self.expected_path = self.get_expected_path()
            print("EXPECTED PATH", self.expected_path)

            self.ckpt_counter = -1
        
        self.log_state_tag()
    
    def initialize(self):
        if self.datasets is not None:
            self.functionObject.initialize(self.datasets, self.channel)

    def checkpoint(self):

        # write logged outputs, state, state_tag to reliable storage

        state = {"latest_input_received": self.latest_input_received, "logged_outputs": self.logged_outputs, "out_seq" : self.out_seq,
        "function_object": self.functionObject.serialize(), "tag":self.state_tag, "target_output_state": self.target_output_state}
        state_str = pickle.dumps(state)
        s3_resource = boto3.resource('s3')
        bucket, key = self.checkpoint_location

        # if this fails we are dead, but probability of this failing much smaller than dump failing
        # the lack of rename in S3 is a big problem
        s3_resource.Object(bucket, key).put(state_str)

        self.truncate_log()
        truncate_tasks = []
        for parent in self.parents:
            for channel in self.parents[parent]:
                handler = self.parents[parent][channel]
                truncate_tasks.append(handler.truncate_logged_outputs.remote(self.id, self.channel, self.state_tag[parent][channel]))
        try:
            ray.get(truncate_tasks)
        except ray.exceptions.RayActorError:
            print("A PARENT HAS FAILED")
            pass
    
    def ask_upstream_for_help(self):
        recover_tasks = []
        print("UPSTREAM",self.parents)
        for parent in self.parents:
            for channel in self.parents[parent]:
                handler = self.parents[parent][channel]
                recover_tasks.append(handler.help_downstream_recover.remote(self.id, self.state_tag[parent][channel]))
        ray.get(recover_tasks)
        
    def get_batches(self, mailbox, mailbox_id):
        while True:
            message = self.p.get_message()
            if message is None:
                break
            if message['channel'].decode('utf-8') == "mailbox-" + str(self.id) + "-" + str(self.channel):
                mailbox.append(message['data'])
            elif message['channel'].decode('utf-8') ==  "mailbox-id-" + str(self.id)+ "-" + str(self.channel):
                # this should be a tuple (source_id, source_tag)
                mailbox_id.append(pickle.loads(message['data']))
        
        batches_returned = 0
        while len(mailbox) > 0 and len(mailbox_id) > 0:
            first = mailbox.popleft()
            stream_id, channel,  tag = mailbox_id.popleft()

            if tag <= self.state_tag[stream_id][channel]:
                print("rejected an input stream's tag smaller than or equal to current state tag")
                continue
            if tag > self.latest_input_received[stream_id][channel] + 1:
                print("DROPPING INPUT. THIS IS A FUTURE INPUT THAT WILL BE RESENT (hopefully)")
                continue

            batches_returned += 1
            self.latest_input_received[stream_id][channel] = tag
            if len(first) < 20 and pickle.loads(first) == "done":
                # the responsibility for checking how many executors this input stream has is now resting on the consumer.
                self.parents[stream_id].pop(channel)
                if len(self.parents[stream_id]) == 0:
                    self.parents.pop(stream_id)
                print("done", stream_id)
            else:
                self.buffered_inputs[stream_id][channel].append(pickle.loads(first))
            
        return batches_returned
    
    def get_expected_path(self):
        return deque([pickle.loads(i) for i in self.r.lrange("state-tag-" + str(self.id) + "-" + str(self.channel), 0, self.r.llen("state-tag-" + str(self.id)))])
    
    # truncate the log to the checkpoint
    def truncate_log(self):
        while True:
            if self.r.llen("state-tag-" + str(self.id)) == 0:
                raise Exception
            tag = pickle.loads(self.r.lpop("state-tag-" + str(self.id)))
            
            if np.product(tag == self.state_tag) == 1:
                return

    def schedule_for_execution(self):
        if len(self.expected_path) == 0:
            # process the source with the most backlog
            lengths = {(i,j): len(self.buffered_inputs[i][j]) for i in self.buffered_inputs for j in self.buffered_inputs[i]}
            #print(lengths)
            parent, channel = max(lengths, key=lengths.get)
            length = lengths[(parent,channel)]
            if length == 0:
                return None, None

            # now drain that source
            batch = pd.concat(self.buffered_inputs[parent][channel])
            self.state_tag[parent][channel] += length
            self.buffered_inputs[parent][channel].clear()
            self.log_state_tag()
            return parent, batch

        else:
            expected = self.expected_path[0]
            diffs = {(i,j): expected[i][j] - self.state_tag[i][j] for i in expected for j in expected[i]}
            # there should only be one nonzero value in diffs. we need to figure out which one that is.
            to_do = None
            for key in diffs:
                if diffs[key] > 0:
                    if to_do is None:
                        to_do = key
                    else:
                        raise Exception("shouldn't have more than one source > 0")
            
            parent, channel = to_do
            required_batches = diffs[(parent, channel)]
            if len(self.buffered_inputs[parent][channel]) < required_batches:
                # cannot fulfill expectation
                print("CANNOT FULFILL EXPECTATION")
                return None, None
            else:
                batch = pd.concat([self.buffered_inputs[parent][channel].popleft() for i in range(required_batches)])
            self.state_tag = expected
            self.expected_path.popleft()
            self.log_state_tag()
            return parent, batch

    def input_buffers_drained(self):
        for parent in self.buffered_inputs:
            for channel in self.buffered_inputs[parent]:
                if len(self.buffered_inputs[parent][channel]) > 0:
                    return False
        return True
    
@ray.remote
class NonBlockingTaskNode(TaskNode):
    def __init__(self, id, channel,  mapping, datasets, functionObject, parents, checkpoint_location, checkpoint_interval = 10, ckpt = None) -> None:
        super().__init__(id, channel,  mapping, datasets, functionObject, parents, checkpoint_location, checkpoint_interval , ckpt )
    
    def execute(self):
        
        mailbox = deque()
        mailbox_meta = deque()

        while not (len(self.parents) == 0 and self.input_buffers_drained()):

            # append messages to the mailbox
            batches_returned = self.get_batches(mailbox, mailbox_meta)
            if batches_returned == 0:
                continue
            # deque messages from the mailbox in a way that makes sense
            stream_id, batch = self.schedule_for_execution()

            if stream_id is None:
                continue

            print(self.state_tag)

            results = self.functionObject.execute( stream_id, batch)
            
            self.ckpt_counter += 1
            if self.ckpt_counter % self.checkpoint_interval == 0:
                print(self.id, "CHECKPOINTING")
                self.checkpoint()

            # this is a very subtle point. You will only breakout if length of self.target, i.e. the original length of 
            # target list is bigger than 0. So you had somebody to send to but now you don't

            if results is not None and len(self.targets) > 0:
                break_out = False
                assert type(results) == list                    
                for result in results:
                    if self.push(result) is False:
                        break_out = True
                        break
                if break_out:
                    break
            else:
                pass
        
        obj_done =  self.functionObject.done(self.channel) 
        del self.functionObject
        gc.collect()
        if obj_done is not None:
            self.push(obj_done)
        
        self.done()
        self.r.publish("node-done-"+str(self.id),str(self.channel))
    
@ray.remote
class BlockingTaskNode(TaskNode):
    def __init__(self, id, channel,  mapping, datasets, output_dataset, functionObject, parents, checkpoint_location, checkpoint_interval = 10, ckpt = None) -> None:
        super().__init__(id, channel,  mapping, datasets, functionObject, parents, checkpoint_location, checkpoint_interval , ckpt )
        self.output_dataset = output_dataset
    
    # explicit override with error. Makes no sense to append to targets for a blocking node. Need to use the dataset instead.
    def append_to_targets(self,tup):
        raise Exception("Trying to stream from a blocking node")
    
    def execute(self):
        
        mailbox = deque()
        mailbox_meta = deque()

        while not (len(self.parents) == 0 and self.input_buffers_drained()):

            # append messages to the mailbox
            batches_returned = self.get_batches(mailbox, mailbox_meta)
            if batches_returned == 0:
                continue
            # deque messages from the mailbox in a way that makes sense
            stream_id, batch = self.schedule_for_execution()

            if stream_id is None:
                continue

            print(self.state_tag)

            results = self.functionObject.execute( stream_id, batch)
            
            self.ckpt_counter += 1
            if self.ckpt_counter % self.checkpoint_interval == 0:
                print(self.id, "CHECKPOINTING")
                self.checkpoint()

            # this is a very subtle point. You will only breakout if length of self.target, i.e. the original length of 
            # target list is bigger than 0. So you had somebody to send to but now you don't

            if results is not None and len(results) > 0:
                assert type(results) == list
                for result in results:
                    key = str(self.id) + "-" + str(self.channel) + "-" + str(self.object_count)
                    self.object_count += 1
                    self.r.set(key, pickle.dumps(result))
                    self.output_dataset.added_object.remote(self.channel, (ray.util.get_node_ip_address(), key, sys.getsizeof(result)))                    
            else:
                pass
        
        obj_done =  self.functionObject.done(self.channel) 
        del self.functionObject
        gc.collect()
        if obj_done is not None:
            self.push(obj_done)
        
        self.done()
        self.r.publish("node-done-"+str(self.id),str(self.channel))


r = redis.Redis(host="localhost",port=6800,db=0)
r.flushall()
input_actors_a = {k: InputActor.options(max_concurrency = 2, num_cpus = 0.001).remote(k,k,"yugan","a-big.csv",["key"] + ["avalue" + str(i) for i in range(100)]) for k in range(NUM_MAPPERS)}
input_actors_b = {k: InputActor.options(max_concurrency = 2, num_cpus = 0.001).remote(k + NUM_MAPPERS,k,"yugan","b-big.csv",["key"] + ["bvalue" + str(i) for i in range(100)]) for k in range(NUM_MAPPERS)}
parents = {**input_actors_a, **{i + NUM_MAPPERS: input_actors_b[i] for i in input_actors_b}}
join_actors = {i: Actor.options(max_concurrency = 2, num_cpus = 0.001).remote(i,parents) for i in range(NUM_MAPPERS * 2, NUM_MAPPERS * 2 + NUM_JOINS)}

def partition_fn(data, target):
    if type(data) == str and data == "done":
        return "done"
    else:
        return data[data.key % NUM_JOINS == target - NUM_MAPPERS * 2]

for i in range(NUM_MAPPERS * 2, NUM_MAPPERS * 2 + NUM_JOINS):
    for j in range(NUM_MAPPERS):
        ray.get(input_actors_a[j].append_to_targets.remote((i, partition_fn)))
        ray.get(input_actors_b[j].append_to_targets.remote((i, partition_fn)))

handlers = {}
for i in range(NUM_MAPPERS):
    handlers[i] = input_actors_a[i].execute.remote()
    handlers[i + NUM_MAPPERS] = input_actors_b[i].execute.remote()
for i in range(NUM_MAPPERS * 2, NUM_MAPPERS * 2 + NUM_JOINS):
    handlers[i] = join_actors[i].execute.remote()

def no_failure_test():
    ray.get(list(handlers.values()))

def actor_failure_test():
    time.sleep(1)

    to_dies = [NUM_MAPPERS * 2, NUM_MAPPERS * 2 + 2]

    for to_die in to_dies:
        ray.kill(join_actors[to_die])
        join_actors.pop(to_die)

    # immediately restart this actor
    for actor_id in to_dies:
        join_actors[actor_id] = Actor.options(max_concurrency = 2, num_cpus = 0.001).remote(actor_id,parents,"ckpt-" + str(actor_id) + ".pkl")
        handlers.pop(actor_id)

    helps = []
    for actor_id in to_dies:
        helps.append(join_actors[actor_id].ask_upstream_for_help.remote())
    ray.get(helps)

    for actor_id in to_dies:
        handlers[actor_id] = (join_actors[actor_id].execute.remote())
    ray.get(list(handlers.values()))

def input_actor_failure_test():
    time.sleep(1)
    to_dies = [0,1]

    for to_die in to_dies:
        ray.kill(input_actors_a[to_die])
        input_actors_a.pop(to_die)

    # immediately restart this actor
    for actor_id in to_dies:
        input_actors_a[actor_id] = InputActor.options(max_concurrency = 2, num_cpus = 0.001).remote(actor_id,actor_id,"yugan","a-big.csv",["key"] + ["avalue" + str(i) for i in range(100)],"ckpt-" + str(actor_id) + ".pkl" ) 
        handlers.pop(actor_id)
    
    appends = []
    for actor_id in to_dies:
        for i in range(NUM_MAPPERS * 2, NUM_MAPPERS * 2 + NUM_JOINS):
            appends.append(input_actors_a[actor_id].append_to_targets.remote((i, partition_fn)))
    ray.get(appends)

    for actor_id in to_dies:
        handlers[actor_id] = (input_actors_a[actor_id].execute.remote())
    ray.get(list(handlers.values()))

def correlated_failure_test():
    time.sleep(3)
    input_to_dies = [0,1]
    actor_to_dies = [NUM_MAPPERS * 2, NUM_MAPPERS * 2 + 2]
    for to_die in actor_to_dies:
        ray.kill(join_actors[to_die])
        join_actors.pop(to_die)
    for to_die in input_to_dies:
        ray.kill(input_actors_a[to_die])
        input_actors_a.pop(to_die)
    
    for actor_id in input_to_dies:
        input_actors_a[actor_id] = InputActor.options(max_concurrency = 2, num_cpus = 0.001).remote(actor_id,actor_id,"yugan","a-big.csv",["key"] + ["avalue" + str(i) for i in range(100)],"ckpt-" + str(actor_id) + ".pkl" ) 
        handlers.pop(actor_id)
        parents[actor_id] = input_actors_a[actor_id]
    for actor_id in actor_to_dies:
        join_actors[actor_id] = Actor.options(max_concurrency = 2, num_cpus = 0.001).remote(actor_id,parents,"ckpt-" + str(actor_id) + ".pkl")
        handlers.pop(actor_id)
    
    appends = []
    for actor_id in input_to_dies:
        for i in range(NUM_MAPPERS * 2, NUM_MAPPERS * 2 + NUM_JOINS):
            appends.append(input_actors_a[actor_id].append_to_targets.remote((i, partition_fn)))
    ray.get(appends)

    helps = []
    for actor_id in actor_to_dies:
        helps.append(join_actors[actor_id].ask_upstream_for_help.remote())
    ray.get(helps)

    for actor_id in actor_to_dies:
        handlers[actor_id] = (join_actors[actor_id].execute.remote())
    for actor_id in input_to_dies:
        handlers[actor_id] = (input_actors_a[actor_id].execute.remote())
    ray.get(list(handlers.values()))

#no_failure_test()
#actor_failure_test() # there is some problem with non-contiguous state variables that only occur with some runs. Need to track those down.
#input_actor_failure_test()
correlated_failure_test()