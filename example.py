
import os
import json
from urllib import request

def claim(count=1, server="claims", timeout=0, resource="gpu"):

    try:
        if timeout <= 0:
            response = json.loads(request.urlopen("http://{}/request?{}={}".format(server, resource, count)).read())
        else:
            response = json.loads(request.urlopen("http://{}/wait?{}={}".format(server, resource, count), timeout=timeout).read())

        ids = [str(device["info"]["number"]) for device in response]
        if ids:
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(ids)
            return True

    finally:
        pass

    return False

if __name__ == "__main__":
    claim(4, "172.17.0.11", 30)