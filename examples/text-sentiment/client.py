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

# Third Party
from start_runtime import protocol_arg
import grpc
import requests

# Local
from caikit.runtime.service_factory import ServicePackageFactory
from text_sentiment.data_model import TextInput

if __name__ == "__main__":

    inference_service = ServicePackageFactory().get_service_package(
        ServicePackageFactory.ServiceType.INFERENCE,
    )

    protocol = protocol_arg()

    model_id = "text_sentiment"

    if protocol == "grpc":
        # Setup the client
        port = 8085
        channel = grpc.insecure_channel(f"localhost:{port}")
        client_stub = inference_service.stub_class(channel)

        # Run inference for two sample prompts
        for text in ["I am not feeling well today!", "Today is a nice sunny day"]:
            input_text_proto = TextInput(text=text).to_proto()
            request = inference_service.messages.HuggingFaceSentimentTaskRequest(
                text_input=input_text_proto
            )
            response = client_stub.HuggingFaceSentimentTaskPredict(
                request, metadata=[("mm-model-id", model_id)], timeout=1
            )
            print("Text:", text)
            print("RESPONSE:", response)

    elif protocol == "http":
        port = 8080
        # Run inference for two sample prompts
        for text in ["I am not feeling well today!", "Today is a nice sunny day"]:
            payload = {"inputs": {"text_input": {"text": text}}}
            response = requests.post(
                f"http://localhost:{port}/api/v1/{model_id}/task/hugging-face-sentiment",
                json=payload,
                timeout=1,
            )
            print("Text:", text)
            print("RESPONSE:", response.json())

    else:
        print("--protocol must be one of [grpc, http]")
