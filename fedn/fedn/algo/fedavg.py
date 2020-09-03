import json
import os
import queue
import tempfile
import time

import fedn.common.net.grpc.fedn_pb2 as fedn
import tensorflow as tf
from fedn.utils.helpers import KerasSequentialHelper
from threading import Thread, Lock

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)


class FEDAVGCombiner:
    """ A Local SGD / Federated Averaging (FedAvg) combiner. """

    def __init__(self, id, storage, server):

        # super().__init__(address, port, id, role)
        self.run_configs_lock = Lock()
        self.run_configs = []
        self.storage = storage
        self.id = id
        self.model_id = None
        self.server = server

        self.config = {}
        # TODO: Use MongoDB
        self.validations = {}

        # TODO: make choice of helper configurable
        self.helper = KerasSequentialHelper()
        # Queue for model updates to be processed.
        self.model_updates = queue.Queue()

    def get_model_id(self):
        return self.model_id

    def report_status(self, msg, log_level=fedn.Status.INFO, type=None, request=None, flush=True):
        print("COMBINER({}):{} {}".format(self.id, log_level, msg), flush=flush)

    def receive_model_candidate(self, model_id):
        """ Callback when a new model version is reported by a client. 
            We simply put the model_id on a queue to be processed later. """
        try:
            self.report_status("COMBINER: callback received model {}".format(model_id),
                               log_level=fedn.Status.INFO)
            # TODO - here would be a place to do some additional validation of the model contribution. 
            self.model_updates.put(model_id)
        except Exception as e:
            self.report_status("COMBINER: Failed to receive candidate model! {}".format(e),
                               log_level=fedn.Status.WARNING)
            print("Failed to receive candidate model!")
            pass

    def receive_validation(self, validation):
        """ Callback for a validation request """

        # TODO: Track this in a DB
        model_id = validation.model_id
        data = json.loads(validation.data)
        try:
            self.validations[model_id].append(data)
        except KeyError:
            self.validations[model_id] = [data]

        self.report_status("COMBINER: callback processed validation {}".format(validation.model_id),
                           log_level=fedn.Status.INFO)

    def combine_models(self, nr_expected_models=None, timeout=120):
        """ Compute an iterative/running average of models arriving to the combiner. """

        round_time = 0.0
        print("COMBINER: combining model updates...")

        # First model in the update round
        try:
            model_id = self.model_updates.get(timeout=timeout)
            print("Combining by getting model {}".format(model_id), flush=True)
            # Fetch the model data blob from storage
            import sys
            model_str = self.server.get_model(model_id)
            tries = 0
            while tries < 3:
                tries += 1
                if not model_str or sys.getsizeof(model_str) == 80:
                    print("Model download failed. retrying", flush=True)
                    import time
                    time.sleep(1.0)
                    model_str = self.server.get_model(model_id)

            import sys
            #print("now writing {}".format(sys.getsizeof(model_str.getbuffer())), flush=True)
            model = self.helper.load_model(model_str.getbuffer())
            nr_processed_models = 1
            self.model_updates.task_done()
        except queue.Empty as e:
            self.report_status("COMBINER: training round timed out.", log_level=fedn.Status.WARNING)
            return None

        while nr_processed_models < nr_expected_models:
            try:
                model_id = self.model_updates.get(block=False)
                self.report_status("Received model update with id {}".format(model_id))

                model_next = self.helper.load_model(self.server.get_model(model_id).getbuffer())
                self.helper.increment_average(model, model_next, nr_processed_models)

                nr_processed_models += 1
                self.model_updates.task_done()
            except Exception as e:
                self.report_status("COMBINER failcode: {}".format(e))
                time.sleep(1.0)
                round_time += 1.0

            if round_time >= timeout:
                self.report_status("COMBINER: training round timed out.", log_level=fedn.Status.WARNING)
                print("COMBINER: Round timed out.")
                return None

        self.report_status("ORCHESTRATOR: Training round completed, combined {} models.".format(nr_processed_models),
                           log_level=fedn.Status.INFO)
        print("DONE, combined {} models".format(nr_processed_models))
        return model

    def __assign_clients(self, n):
        """  Obtain a list of clients to talk to in a round. """

        # TODO: If we want global sampling without replacement the server needs to assign clients
        active_trainers = self.server.get_active_trainers()

        # If the number of requested trainers exceeds the number of available, use all available. 
        if n > len(active_trainers):
            n = len(active_trainers)

        import random
        self.trainers = random.sample(active_trainers, n)
        # TODO: In the general case, validators could be other clients as well
        self.validators = self.trainers

    def __training_round(self):

        # We flush the queue at a beginning of a round (no stragglers allowed)
        # TODO: Support other ways to handle stragglers. 
        with self.model_updates.mutex:
            self.model_updates.queue.clear()

        self.report_status("COMBINER: Initiating training round, participating members: {}".format(self.trainers))
        self.server.request_model_update(self.model_id, clients=self.trainers)

        # Apply combiner
        model = self.combine_models(nr_expected_models=len(self.trainers), timeout=int(self.config['round_timeout']))
        return model

    def __validation_round(self):
        self.server.request_model_validation(self.model_id, from_clients=self.validators)

    def push_run_config(self, plan):
        self.run_configs_lock.acquire()
        self.run_configs.append(plan)
        self.run_configs_lock.release()

    def run(self):

        import time
        try:
            while True:
                time.sleep(1)
                print("COMBINER: FEDAVG exec loop",flush=True)
                self.run_configs_lock.acquire()
                if len(self.run_configs) > 0:
                    plan = self.run_configs.pop()
                    self.run_configs_lock.release()
                    self.exec(plan)

                if self.run_configs_lock.locked():
                    self.run_configs_lock.release()

        except (KeyboardInterrupt, SystemExit):
            pass

    def exec(self, config):
        """ Coordinates training and validation tasks with clints, as specified in the 
            config (CombinerConfiguration) """

        self.config = config
        self.model_id = self.config['model_id']

        print("COMBINER starting from model {}".format(self.model_id))

        # Fetch the input model blob from storage and load in local memory
        timeout_retry = 3
        import time
        tries = 0
        while True:
            try:
                model = self.storage.get_model_stream(self.model_id)
                if model:
                    break
            except Exception as e:
                print("COMBINER could not fetch model from bucket. retrying in {}".format(timeout_retry), flush=True)
                time.sleep(timeout_retry)
                tries += 1
                if tries > 2:
                    print("COMBINER exiting. could not fetch seed model.")
                    return

        self.server.set_model(model, self.model_id)

        # Check that the minimal number of required clients to start a round are connected 
        import time
        ready = False
        while not ready:
            active = self.server.nr_active_trainers()
            if active >= int(config['clients_required']):
                ready = True
            else:
                print("waiting for {} clients to get started, currently: {}".format(int(config['clients_required']) - active,
                                                                                    active), flush=True)
            time.sleep(1)

        # Execute the configured number of rounds
        for r in range(1, int(config['rounds']) + 1):
            print("STARTING ROUND {}".format(r), flush=True)
            print("\t FEDAVG: Starting training round {}".format(r), flush=True)

            self.__assign_clients(int(config['clients_requested']))
            model = self.__training_round()

            if model:
                print("\t FEDAVG: Round completed.", flush=True)

                # TODO: Use configuration to decide if we use a scratchspace to checkpoint the model. 
                fod, outfile_name = tempfile.mkstemp(suffix='.h5')
                model.save(outfile_name)
                # Upload new model to storage repository (persistent)
                # and save to local storage for sharing with clients.

                # TODO: Refactor - Checkpointing in the configured combiner-private storage 
                # should be handled by self.set_model probably. 
                model_id = self.storage.set_model(outfile_name, is_file=True)
                from io import BytesIO
                a = BytesIO()
                a.seek(0, 0)
                with open(outfile_name, 'rb') as f:
                    a.write(f.read())

                # Stream aggregated model to server 
                # TODO: Not strictly necessary to stream model here, can be slight waste of resources.
                self.server.set_model(a, model_id)
                os.unlink(outfile_name)

                self.model_id = model_id

                print("...done. New aggregated model: {}".format(self.model_id))

                print("\t Starting validation round {}".format(r))
                self.__validation_round()

                print("------------------------------------------")
                print("FEDAVG: ROUND COMPLETED.", flush=True)
                print("\n")
            else:
                print("\t Failed to update global model in round {0}!".format(r))