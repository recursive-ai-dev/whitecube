"""Inject a standard URI-list drop into one isolated client connection.

The real compositor continues to own window creation, buffers, and rendering.
Only the test data offer is handled here, including its actual pipe transfer.
"""
import collections
import os
import struct
import time
from wayland_proxy import WaylandProxy, message, u32, string_at, wait_for

def wire_string(text):
    data=text.encode()+b'\0'
    return u32(len(data))+data+b'\0'*((-len(data))%4)

class InputProxy(WaylandProxy):
    def __init__(self,*args):
        self.wire_bytes=b''
        self.wire_fds=collections.deque()
        self.offer_id=0xff000000
        self.uri_data=b''
        self.drop_received=False
        super().__init__(*args)

    def client_request(self,data,fds):
        self.wire_bytes+=data
        self.wire_fds.extend(os.dup(fd) for fd in fds)
        while len(self.wire_bytes)>=8:
            obj,word=struct.unpack_from('=II',self.wire_bytes)
            length,opcode=word>>16,word&65535
            if len(self.wire_bytes)<length:
                return
            packet,self.wire_bytes=self.wire_bytes[:length],self.wire_bytes[length:]
            body=packet[8:]
            fake=obj==self.offer_id
            takes_fd=(fake and opcode==1) or (self.objects.get(obj)=='wl_shm' and opcode==0)
            descriptor=self.wire_fds.popleft() if takes_fd else None
            try:
                if fake:
                    if opcode==1:
                        mime,_=string_at(body)
                        assert mime=='text/uri-list'
                        os.write(descriptor,self.uri_data)
                        self.drop_received=True
                else:
                    out_fds=[] if descriptor is None else [descriptor]
                    self.inspect_client(packet,out_fds)
                    self.forward(self.server,packet,out_fds)
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def drop(self,uris):
        wait_for(lambda:self.device and self.main_surface,'Wayland data device')
        self.uri_data=('\r\n'.join(uris)+'\r\n').encode()
        self.serial+=1
        self.send_events([
            message(self.device,0,u32(self.offer_id)),
            message(self.offer_id,0,wire_string('text/uri-list')),
            message(self.device,1,u32(self.serial,self.main_surface,120*256,130*256,self.offer_id)),
            message(self.device,4),
            message(self.device,2),
        ])
        wait_for(lambda:self.drop_received,'URI-list pipe read')

    def click(self,x,y):
        wait_for(lambda:self.pointer and self.main_surface,'Wayland pointer')
        self.serial+=1
        stamp=int(time.monotonic()*1000)&0xffffffff
        self.send_events([
            message(self.pointer,0,u32(self.serial,self.main_surface,int(x*256),int(y*256))),
            message(self.pointer,2,u32(stamp,int(x*256),int(y*256))),
            message(self.pointer,3,u32(self.serial,stamp,272,1)),
            message(self.pointer,3,u32(self.serial+1,stamp+1,272,0)),
            message(self.pointer,5),
        ])

    def close(self):
        super().close()
        while self.wire_fds:
            os.close(self.wire_fds.popleft())
