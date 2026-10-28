"""NVIDIA monitoring indices are not Linux /dev/nvidia minor numbers."""
from unittest.mock import patch
import pytest
from blinker import signal
from patroller.gpu import GPUMonitor, PMon

XML = b'''<nvidia_smi_log>
<gpu id="0000:05:00.0"><product_name>Test GPU A</product_name><product_brand>Test</product_brand><product_architecture>Pascal</product_architecture><uuid>GPU-A</uuid><minor_number>3</minor_number><fb_memory_usage><total>11000 MiB</total></fb_memory_usage></gpu>
<gpu id="0000:09:00.0"><product_name>Test GPU B</product_name><product_brand>Test</product_brand><product_architecture>Pascal</product_architecture><uuid>GPU-B</uuid><minor_number>1</minor_number><fb_memory_usage><total>11000 MiB</total></fb_memory_usage></gpu>
</nvidia_smi_log>'''


def monitor(indexes=b'0, GPU-A\n2, GPU-B\n'):
    def output(command):
        return indexes if '--query-gpu=index,uuid' in command else XML
    with patch('patroller.gpu.subprocess.check_output', side_effect=output):
        return GPUMonitor()


def test_pmon_index_resolves_actual_uuid_not_linux_minor_number():
    m = monitor()
    assert m.find(0).uuid == 'GPU-A'
    assert m.find(2).uuid == 'GPU-B'
    assert m.find(0).info()['number'] == 0
    assert m.find(2).info()['number'] == 2
    events = []
    def access(sender, **kwargs):
        events.append(kwargs)
    signal('access').connect(access, weak=False)
    try:
        parser = PMon(m)
        parser.line('0 12345 C 0 0 - - - - python')
        parser.line('2 23456 C 0 0 - - - - python')
        assert events == [dict(device='GPU-A', process=12345), dict(device='GPU-B', process=23456)]
    finally:
        signal('access').disconnect(access)


@pytest.mark.parametrize('indexes', [b'0, GPU-A\n', b'0, GPU-A\n0, GPU-B\n',
    b'0, GPU-A\n1, GPU-A\n', b'0, GPU-A\n2, GPU-OTHER\n'])
def test_mismatched_or_duplicate_gpu_index_mapping_is_rejected(indexes):
    with pytest.raises((RuntimeError, ValueError)):
        monitor(indexes)
