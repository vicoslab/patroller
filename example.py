
import os
from urllib import request, error
from socket import error as socket_error
import json

def claim(count=1, server="claims", timeout=0):

    try:
        if timeout <= 0:
            response = json.loads(request.urlopen("http://{}/request?gpu={}".format(server, count)).read())
        else:
            response = json.loads(request.urlopen("http://{}/wait?gpu={}".format(server, count), timeout=timeout).read())

        ids = [str(device["info"]["number"]) for device in response]
        if ids:
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(ids)
            return True
        else:
            return False

    except error.HTTPError:
        return False
    except socket_error:
        return False

if __name__ == "__main__":
    claim(4, "172.17.0.11", 30)