# Copyright The Caikit Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Standard
from importlib.metadata import version
from typing import Optional, Type
from uuid import uuid4
import abc
import concurrent.futures
import importlib
import multiprocessing
import os
import re
import threading
import traceback

# Third Party
from google.protobuf.descriptor import FieldDescriptor
from grpc import StatusCode
import grpc

# First Party
import alog

# Local
from caikit import get_config
from caikit.core.module import ModuleBase
from caikit.interfaces.runtime.data_model import TrainingJob
from caikit.runtime.model_management.model_manager import ModelManager
from caikit.runtime.model_management.training_manager import TrainingManager
from caikit.runtime.service_factory import ServicePackage
from caikit.runtime.types.caikit_runtime_exception import CaikitRuntimeException
from caikit.runtime.utils.import_util import clean_lib_names, get_data_model
from caikit.runtime.utils.servicer_util import (
    build_caikit_library_request_dict,
    snake_to_upper_camel,
    validate_data_model,
)
from caikit.runtime.work_management.destroyable_thread import DestroyableThread
import caikit.core

log = alog.use_channel("GT-SERVICR-I")
error = caikit.core.toolkit.errors.error_handler.get(log)

# Protobuf non primitives
# Ref: https://developers.google.com/protocol-buffers/docs/reference/cpp/google.protobuf.descriptor
NON_PRIMITIVE_TYPES = [FieldDescriptor.TYPE_MESSAGE, FieldDescriptor.TYPE_ENUM]

OOM_EXIT_CODE = 137

# pylint: disable=too-many-instance-attributes
class GlobalTrainServicer:
    """Something something about the train servicer"""

    def __init__(self, training_service: ServicePackage):
        self._training_service = training_service
        self._model_manager = ModelManager.get_instance()
        self.training_manager = TrainingManager.get_instance()
        # NOTE: we are using ThreadPoolExecutor for simplicity of the
        # the API with an intent to handle the training job
        # in an async fashion with "Futures".
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # store the map of model ids to job ids
        self.training_map = self.training_manager.training_futures
        caikit_config = get_config()
        self.training_output_dir = caikit_config.runtime.training.output_dir
        self.auto_load_trained_model = (
            caikit_config.runtime.training.auto_load_trained_model
        )
        self.use_subprocess = caikit_config.runtime.training.use_subprocess

        # TODO: think about if we really want to do this here:
        self.cdm = get_data_model()

        # Validate that the Caikit Library CDM is compatible with our service descriptor
        validate_data_model(self._training_service.descriptor)
        log.info("<RUN76773777I>", "Validated Caikit Library CDM successfully")

        # TODO: support multiple libs? `caikit_config.libraries` dict
        # Or grab the `libraries` off of the `training_service` instead of config here?
        # Duplicate code in global_train_servicer
        # pylint: disable=duplicate-code
        self.library = clean_lib_names(caikit_config.runtime.library)[0]
        try:
            lib_version = version(self.library)
        except Exception:  # pylint: disable=broad-exception-caught
            lib_version = "unknown"

        log.info(
            "<RUN76884779I>",
            "Constructed train service for library: %s, version: %s",
            self.library,
            lib_version,
        )
        super()

    def Train(self, request, context, *_, **__) -> TrainingJob:
        """Global predict RPC -- Mocks the invocation of a Caikit Library block.train()
        method for a loaded Caikit Library model
        Args:
            request(object):
                A deserialized RPC request message
            context(grpc.ServicerContext): Context object (contains request metadata, etc)
        Returns:
            caikit.interfaces.runtime.data_model.TrainingJob:
                A TrainingJob data model response object
        """
        desc_name = request.DESCRIPTOR.name
        outer_scope_name = "GlobalTrainServicer.Train:%s" % desc_name

        try:
            with alog.ContextLog(log.debug, outer_scope_name):
                # BlocksSampleTaskSampleBlockTrainRequest
                # getattr(importlib.import_module("sample_lib.blocks.sample_task"), "SampleBlock")
                # TODO: fixme - temporary workaround for now
                desc_name = desc_name.replace("TrainRequest", "")
                split = re.split("(?<=.)(?=[A-Z])", desc_name)
                model = None
                try:
                    model = getattr(
                        importlib.import_module(
                            f"{self.library}.{split[0].lower()}.{split[1].lower()}"
                        ),
                        f"{''.join(split[2:])}",
                    )

                except Exception:  # pylint: disable=broad-exception-caught
                    for mod in caikit.core.MODULE_REGISTRY.values():
                        module_split = mod.__module__.split(".")
                        train_request_for_mod = snake_to_upper_camel(
                            f"{module_split[1]}_{module_split[2]}_{mod.__name__}"
                        )
                        if train_request_for_mod == desc_name:
                            model = mod
                            break

                # At this point, if model is still None, we don't know the module this request
                # is for
                if model is None:
                    raise CaikitRuntimeException(
                        StatusCode.INTERNAL,
                        "Global Train not able to parse module for this Train Request",
                    )
                # generate a unique training id
                training_id = str(uuid4())
                return self.run_training_job(
                    request=request,
                    model=model,
                    training_id=training_id,
                    training_output_dir=self.training_output_dir,
                    context=context,
                )

        except CaikitRuntimeException as e:
            log_dict = {
                "log_code": "<RUN50530380W>",
                "message": e.message,
                "error_id": e.id,
            }
            log.warning({**log_dict, **e.metadata})
            raise e

        except concurrent.futures.CancelledError as err:
            log_dict = {
                "log_code": "<RUN71530128W>",
                "message": err.message,
                "error_id": err.id,
            }
            log.warning({**log_dict, **e.metadata})
            raise e

        # Duplicate code in global_predict_servicer
        # pylint: disable=duplicate-code
        except (TypeError, ValueError) as e:
            log_dict = {
                "log_code": "<RUN490439039W>",
                "message": repr(e),
                "stack_trace": traceback.format_exc(),
            }
            log.warning(log_dict)
            raise CaikitRuntimeException(
                StatusCode.INVALID_ARGUMENT,
                f"Exception raised during inference. This may be a problem with your input: {e}",
            ) from e

        except Exception as e:
            log_dict = {
                "log_code": "<RUN49049070W>",
                "message": repr(e),
                "stack_trace": traceback.format_exc(),
            }
            log.warning(log_dict)
            raise CaikitRuntimeException(
                StatusCode.INTERNAL, "Unhandled exception during training"
            ) from e

    def run_training_job(
        self,
        request,
        model,
        training_id,
        training_output_dir,
        context,
        wait=False,
    ) -> TrainingJob:
        """Builds the request dict for calling the train function asynchronously,
        then returns the thread id"""

        # Figure out where this model will be saved
        model_name = request.model_name
        model_path = self._get_model_path(training_output_dir, model_name, training_id)

        # Build the full set of kwargs for the train and save call
        kwargs = {
            "module_class": model,
            "model_path": model_path,
            **build_caikit_library_request_dict(request, model.train),
        }

        event = threading.Event()

        # If running with a subprocess, set the target and args accordingly
        target = (
            SubProcessTrainSaveExecutor(event)
            if self.use_subprocess
            else LocalTrainSaveExecutor(event)
        )

        log.debug2(
            "Training with %s",
            "SUBPROCESS" if self.use_subprocess else "MAIN PROCESS",
        )

        # start training asynchronously
        thread_future = self.run_async(
            runnable_executor=target,
            kwargs=kwargs,
            model_name=model_name,
            model_path=model_path,
        )

        self.training_map[training_id] = thread_future

        # Add callback to register cancellation of training
        def cancel_future(*args, **kwargs):
            if thread_future.running() and not event.is_set():
                event.set()
                thread_future.cancel()

        context.add_callback(cancel_future)

        # if requested, block until the training completes
        if wait:
            with alog.ContextTimer(log.debug, "Training %s complete in: ", training_id):
                thread_future.result()

        # return TrainingJob object
        return TrainingJob(
            model_name=request.model_name, training_id=training_id
        ).to_proto()

    def run_async(
        self,
        runnable_executor,
        kwargs,
        model_name,
        model_path,
    ) -> concurrent.futures.Future:
        """Runs the train function in a thread and saves the trained model in a callback"""

        if self.auto_load_trained_model:

            def target(*args, **kwargs):
                runnable_executor.train_and_save_model(*args, **kwargs)
                return self._load_trained_model(model_name, model_path)

        else:
            target = runnable_executor.train_and_save_model

        future = self.executor.submit(target, **kwargs)

        return future

    def _get_model_path(
        self,
        training_output_dir: Optional[str],
        model_name: str,
        training_id: str,
    ) -> str:
        """Get the right output path for a given model"""

        # make sure we create the right path for saving the trained model
        # The path depends on training_output_dir. If it's provided, use it
        # otherwise use the default
        if training_output_dir is not None:
            if training_id in training_output_dir:
                model_path = os.path.join(training_output_dir, model_name)
            else:  # create a subdir with training_id
                model_path = os.path.join(training_output_dir, training_id, model_name)
        else:
            model_path = os.path.join(self.training_output_dir, training_id, model_name)
        return model_path

    def _load_trained_model(self, model_name: str, model_path: str):
        log.debug("Autoloading trained model %s", model_name)
        self._model_manager.load_model(
            model_id=model_name,
            local_model_path=model_path,
            model_type="standalone",
        )
        return self._model_manager.retrieve_model(model_name)


# NOTE: Following would get replaced with training backends potentially
# in future.
# NOTE: Instead of using executors like Local / Subprocess
# it might make sense to instead use concurrent.Future based
# executors, so ThreadPoolExecutors and ProcessPoolExecutors, but
# ProcessPoolExecutor doesn't support `fork` start method
class TrainSaveExecutorBase(abc.ABC):
    def __init__(self, event) -> None:
        self.__event = event

    @abc.abstractmethod
    def train_and_save_model(self, *args, **kwargs):
        """Function to kick off training a model based and saving
        the resultant model
        """

    @abc.abstractmethod
    def cancel(self):
        """Function to abort train and save operation on the executor"""


class LocalTrainSaveExecutor(TrainSaveExecutorBase):
    def __init__(self, event) -> None:
        self.__event = event

    def train_and_save_model(
        self,
        module_class: Type[ModuleBase],
        model_path: str,
        *args,
        **kwargs,
    ):
        """This function performs a single training and can be run inside a
        subprocess if needed
        """

        try:
            # Train it
            with alog.ContextTimer(
                log.debug, "Done training %s in: ", module_class.__name__
            ):
                destroyable_thread = DestroyableThread(
                    self.__event,
                    module_class.train,
                    *args,
                    **kwargs,
                )
                destroyable_thread.run()
                model = destroyable_thread.get_or_throw()

            # Save it
            with alog.ContextTimer(
                log.debug,
                "Done saving %s to %s in: ",
                module_class.__name__,
                model_path,
            ):
                model.save(model_path)

        # Handle errors as CaikitRuntime errors with appropriate error codes
        except CaikitRuntimeException as e:
            log.warning(
                {
                    "log_code": "<RUN555430380W>",
                    "message": e.message,
                    "error_id": e.id,
                    **e.metadata,
                }
            )
            raise e
        except (TypeError, ValueError) as e:
            log.warning(
                {
                    "log_code": "<RUN868639039W>",
                    "message": repr(e),
                    "stack_trace": traceback.format_exc(),
                }
            )
            raise CaikitRuntimeException(
                StatusCode.INVALID_ARGUMENT,
                f"Exception raised during training. This may be a problem with your input: {e}",
            ) from e
        except Exception as e:
            log.warning(
                {
                    "log_code": "<RUN490967039W>",
                    "message": repr(e),
                    "stack_trace": traceback.format_exc(),
                }
            )
            raise CaikitRuntimeException(
                StatusCode.INTERNAL,
                f"Exception raised during training: {e}",
            ) from e

    def cancel(self):
        """Function to abort train and save operation on the executor"""
        # TODO: Figure out what we need to do to cancel local executor


class SubProcessTrainSaveExecutor(LocalTrainSaveExecutor):
    class _ErrorCaptureProcess(multiprocessing.get_context("fork").Process):
        """This class wraps a Process and keeps track of any errors that occur
        during execution

        NOTE: We explicitly use "fork" here for two reasons:
            1. It's faster
            2. Due to the auto-generated classes with stream sources, "spawn"
            can result in missing classes since it performs a full re-import,
            but does not regenerate the service APIs
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.error = None

        def set_args(self, *args, **kwargs):
            self._args = args
            self._kwargs = kwargs

        def run(self, *args, **kwargs):
            try:
                return super().run(*args, **kwargs)

            # Catch any errors thrown within a subprocess so that they can be
            # forwarded to the parent
            # pylint: disable=broad-exception-caught
            except Exception as err:
                self.error = err

    def __init__(self, event) -> None:

        self.proc = self._ErrorCaptureProcess(
            target=super().train_and_save_model,
        )

        self.__proc_id = self.proc.pid
        self.__event = event
        super().__init__(event)

    def train_and_save_model(self, *args, **kwargs):

        # Assign args and kwargs to self.proc
        self.proc.set_args(*args, **kwargs)

        self.proc.start()

        if self.__event.is_set():
            self.cancel()

        self.proc.join()

        # If an error occurred, reraise it here
        # TODO: Make sure the stack trace is preserved
        if self.proc.error is not None:
            if isinstance(self.proc.error, CaikitRuntimeException):
                raise self.proc.error
            raise CaikitRuntimeException(
                grpc.StatusCode.INTERNAL,
                "Error caught in training subprocess",
            ) from self.proc.error

        # If process exited with a non-zero exit code
        if self.proc.exitcode and self.proc.exitcode != os.EX_OK:
            if self.proc.exitcode == OOM_EXIT_CODE:
                exception = CaikitRuntimeException(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    "Training process died with OOM error!",
                )
            else:
                exception = CaikitRuntimeException(
                    grpc.StatusCode.UNKNOWN,
                    f"Training process died with exit code {self.proc.exitcode}",
                )
            raise exception

    def cancel(self):
        self.proc.terminate()
        self.proc.close()

        log.error("<RUN57624710E>", "Training cancelled.")

        raise CaikitRuntimeException(
            StatusCode.CANCELLED,
            f"Training request terminated",
        )
