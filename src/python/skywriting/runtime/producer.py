
import simplejson
import tempfile
import threading
import os
import skywriting.runtime.tcp_server as tcp
import skywriting.runtime.file_watcher as fwt
from skywriting.runtime.pycurl_thread import post_string_noreturn
from skywriting.runtime.block_store import get_own_netloc, producer_filename, commit_file

# Maintains a set of block IDs that are currently being written.
# (i.e. They are in the pre-publish/streamable state, and have not been
#       committed to the block store.)
# They map to the executor which is producing them.
streaming_producers = dict()

class FileOutputContext:

    # may_pipe: Should wait for a direct connection, either via local pipe or direct remote socket
    def __init__(self, refid, subscribe_callback, can_use_fd=False, may_pipe=False):
        self.refid = refid
        self.block_store = block_store
        self.subscribe_callback = subscribe_callback
        self.file_watch = None
        self.subscriptions = []
        self.current_size = None
        self.closed = False
        self.succeeded = None
        self.fifo_name = None
        self.may_pipe = may_pipe
        self.can_use_fd = can_use_fd
        self.direct_write_filename = None
        self.direct_write_fd = None
        self.started = False
        self.pipe_deadline = datetime.now() + timedelta(seconds=5)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def get_filename_or_fd(self):
        if self.may_pipe:
            with self.lock:
                if self.direct_write_filename is None and self.direct_write_fd is None:
                    now = datetime.now()
                    if now < self.pipe_deadline:
                        wait_time = self.pipe_deadline - now
                        wait_secs = float(wait_time.seconds) + (float(wait_time.microseconds) / 10**6)
                        ciel.log("Producer for %s: waiting for a direct consumer" % self.refid, "BLOCKPIPE", logging.INFO)
                        self.cond.wait(wait_secs)
                if self.direct_write_filename is not None:
                    ciel.log("Producer for %s: writing direct to filename %s" % (self.refid, self.direct_write_filename), "BLOCKPIPE", logging.INFO)
                    self.started = True
                    return (self.direct_write_filename, False)
                elif self.direct_write_fd is not None:
                    ciel.log("Producer for %s: writing direct to consumer-supplied FD" % self.refid, "BLOCKPIPE", logging.INFO)
                    return (self.direct_write_fd, True)
                elif self.started:
                    ciel.log("Producer for %s: kicked by a regular-file subscription; using conventional stream-file" % self.refid, "BLOCKPIPE", logging.INFO)
                else:
                    self.started = True
                    ciel.log("Producer for %s: timed out waiting for a consumer; writing to local block store" % self.refid, "BLOCKPIPE", logging.INFO)
        return producer_filename(self.refid)

    def get_stream_ref(self):
        if tcp.tcp_server_active():
            return SW2_SocketStreamReference(self.refid, get_own_netloc(), tcp.aux_listen_port)
        else:
            return SW2_StreamReference(self.refid, location_hints=[get_own_netloc()])

    def rollback(self):
        if not self.closed:
            ciel.log("Rollback output %s" % id, 'BLOCKSTORE', logging.WARNING)
            del streaming_producers[self.refid]
            with self.lock:
                self.closed = True
                self.succeeded = False
            if self.fifo_name is not None:
                try:
                    # Dismiss anyone waiting on this pipe
                    fd = os.open(self.fifo_name, os.O_NONBLOCK | os.O_WRONLY)
                    os.close(fd)
                except:
                    pass
                try:
                    os.remove(self.fifo_name)
                except:
                    pass
            if self.file_watch is not None:
                self.file_watch.cancel()
            for subscriber in self.subscriptions:
                subscriber.result(False)

    def close(self):
        if not self.closed:
            del streaming_producers[self.refid]
            with self.lock:
                self.closed = True
                self.succeeded = True
            if self.direct_write_filename is None and self.direct_write_fd is None:
                bs.commit_producer(self.refid)
            if self.file_watch is not None:
                self.file_watch.cancel()
            self.current_size = os.stat(self.block_store.filename(self.refid)).st_size
            for subscriber in self.subscriptions:
                subscriber.progress(self.current_size)
                subscriber.result(True)

    def get_completed_ref(self):
        if not self.closed:
            raise Exception("FileOutputContext for ref %s must be closed before it is realised as a concrete reference" % self.refid)
        if self.direct_write_filename is not None or self.direct_write_fd is not None:
            return SW2_CompletedReference(self.refid)
        completed_file = producer_filename(self.refid)
        if self.current_size < 1024:
            with open(completed_file, "r") as fp:
                return SWDataValue(self.refid, encode_datavalue(fp.read()))
        else:
            return SW2_ConcreteReference(self.refid, size_hint=self.current_size, location_hints=[get_own_netloc()])

    def update_chunk_size(self):
        self.subscriptions.sort(key=lambda x: x.chunk_size)
        self.file_watch.set_chunk_size(self.subscriptions[0].chunk_size)

    def try_direct_attach_consumer(self, consumer, consumer_filename=None, consumer_fd=None):
        assert consumer_filename is not None or consumer_fd is not None
        if not self.may_pipe:
            return False
        else:
            with self.lock:
                if self.started:
                    ciel.log("Producer for %s: consumer tried to attach, but we've already started writing a file" % self.refid, "BLOCKPIPE", logging.INFO)
                    return False
                if consumer_filename is not None:
                    ciel.log("Producer for %s: writing to consumer-supplied filename %s" % (self.refid, consumer_filename), "BLOCKPIPE", logging.INFO)
                    self.direct_write_filename = consumer_filename
                    consumer.start_direct_write()
                    return True
                elif consumer_fd is not None and self.can_use_fd:
                    ciel.log("Producer for %s: writing to consumer-supplied FD %s" % (self.refid, consumer_fd), "BLOCKPIPE", logging.INFO)
                    self.direct_write_fd = consumer_fd
                    consumer.start_direct_write()
                    return True
                else:
                    self.fifo_name = tempfile.mktemp(prefix="ciel-producer-fifo-")
                    os.mkfifo(fifo_name)
                    self.direct_write_filename = self.fifo_name
                    if consumer_fd is not None:
                        ciel.log("Producer for %s: consumer gave an FD to attach, but we can't use FDs directly. Starting 'cat'" % self.refid, "BLOCKPIPE", logging.INFO)
                        consumer.start_direct_write()
                        subprocess.Popen(["cat < %s" % fifo_name], shell=True, stdout=consumer_fd, close_fds=True)
                        return True
                    else:
                        return self.direct_write_filename
                self.cond.notify_all()

    def get_fifo_filename(self):
        return self.fifo_filename

    def follow_file(self, new_subscriber):
        should_start_watch = False
        if self.current_size is not None:
            new_subscriber.progress(self.current_size)
        if self.file_watch is None:
            ciel.log("Starting watch on output %s" % self.refid, "BLOCKSTORE", logging.INFO)
            self.file_watch = self.subscribe_callback(self)
            should_start_watch = True
        self.update_chunk_size()
        if should_start_watch:
            self.file_watch.start()       

    def subscribe(self, new_subscriber, try_direct=False, consumer_filename=None, consumer_fd=None):

        with self.lock:
            if self.closed:
                if self.current_size is not None:
                    new_subscriber.progress(self.current_size)
                new_subscriber.result(self.succeeded)
                return False
            if self.may_pipe:
                if self.direct_write_filename is not None or self.direct_write_fd is not None:
                    raise Exception("Tried to subscribe to output %s, but it's already being consumed directly! Bug? Or duplicate consumer task?" % self.refid)
                self.started = True
                if try_direct:
                    if self.try_direct_attach_consumer(new_subscriber, consumer_filename, consumer_fd):
                        ret = True
                    else:
                        self.follow_file(new_subscriber)
                        ret = False
                else:
                    self.follow_file(new_subscriber)
                    ret = False
            self.subscriptions.append(new_subscriber)
            self.cond.notify_all()
            return ret

    def unsubscribe(self, subscriber):
        try:
            self.subscriptions.remove(subscriber)
        except ValueError:
            ciel.log("Couldn't unsubscribe %s from output %s: not a subscriber" % (subscriber, self.refid), "BLOCKSTORE", logging.ERROR)
        if len(self.subscriptions) == 0 and self.file_watch is not None:
            ciel.log("No more subscribers for %s; cancelling watch" % self.refid, "BLOCKSTORE", logging.INFO)
            self.file_watch.cancel()
            self.file_watch = None
        else:
            self.update_chunk_size()

    def chunk_size_changed(self, subscriber):
        self.update_chunk_size()

    def size_update(self, new_size):
        self.current_size = new_size
        for subscriber in self.subscriptions:
            subscriber.progress(new_size)

    def __enter__(self):
        return self

    def __exit__(self, exnt, exnv, exntb):
        if not self.closed:
            if exnt is None:
                self.close()
            else:
                ciel.log("FileOutputContext %s destroyed due to exception %s: rolling back" % (self.refid, repr(exnv)), "BLOCKSTORE", logging.WARNING)
                self.rollback()
        return False

def make_local_output(self, id, subscribe_callback=None, may_pipe=False):
    '''
    Creates a file-in-progress in the block store directory.
    '''
    if subscribe_callback is None:
        subscribe_callback = fwt.create_watch
    ciel.log.error('Creating file for output %s' % id, 'BLOCKSTORE', logging.INFO)
    new_ctx = FileOutputContext(id, self, subscribe_callback, may_pipe)
    streaming_producers[id] = new_producer
    dot_filename = producer_filename(id)
    open(dot_filename, 'wb').close()
    return new_ctx

def get_producer_for_id(id):
    return streaming_producers[id]

        