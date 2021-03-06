import threading
import os
import time
import grpc
import requests
import json

from google.protobuf.json_format import MessageToJson, MessageToDict

import fedn.common.net.grpc.fedn_pb2 as alliance
import fedn.common.net.grpc.fedn_pb2_grpc as rpc
import grpc

import pymongo
from fedn.common.storage.db.mongo import connect_to_mongodb


class Client:
    def __init__(self, url, port, name):

        self.name = name
        channel = grpc.insecure_channel(url + ":" + str(port))
        self.connection = rpc.ConnectorStub(channel)
        print("Client: {} connected to {}:{}".format(self.name, url, port), flush=True)

        # Connect to MongoDB 
        try:
            self.mdb = connect_to_mongodb()
            self.collection = self.mdb['status']
        except Exception as e:
            print("FAILED TO CONNECT TO MONGO, {}".format(e),flush=True)
            self.collection = None
            raise 

        threading.Thread(target=self.__listen_to_status_stream, daemon=True).start()

    def __listen_to_status_stream(self):
        r = alliance.ClientAvailableMessage()
        r.sender.name = self.name
        r.sender.role = alliance.OTHER
        for status in self.connection.AllianceStatusStream(r):
            print("MONITOR: Recived status:{}".format(status), flush=True)
            data = MessageToDict(status, including_default_value_fields=True)
            print(data, flush=True)

            # Log to MongoDB
            if self.collection:
                self.collection.insert_one(data)

    def run(self):
        print("starting")
        while True:
            print(".")
            time.sleep(1)

def main():
    url = os.environ['MONITOR_HOST']
    port = os.environ['MONITOR_PORT']
    if url is None or port is None:
        print("Cannot start without MONITOR_HOST and MONITOR_PORT")
        return

    c = Client(url, port, "monitor")
    c.run()


if __name__ == '__main__':
    print("MONITOR: starting up", flush=True)
    main()
