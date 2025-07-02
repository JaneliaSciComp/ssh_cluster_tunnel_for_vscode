import cluster_tunnel
import time

def test_start_job():
    node_and_port = cluster_tunnel.get_compute_node_and_port()
    assert node_and_port == ''

    cluster_tunnel.start_job()

    max_tries = 10
    tries = 0
    while tries < max_tries and not node_and_port:
        time.sleep(1)
        node_and_port = cluster_tunnel.get_compute_node_and_port()
        tries += 1

    assert node_and_port

def test_get_compute_node_and_port():
    node_and_port = cluster_tunnel.get_compute_node_and_port()
    assert node_and_port
    node, port = node_and_port.split(':')
    assert node
    assert port

def test_do_proxy():
    cluster_tunnel.do_proxy()

def test_kill_job():
    node_and_port = cluster_tunnel.get_compute_node_and_port()
    assert node_and_port != ''

    cluster_tunnel.kill_job()

    max_tries = 10
    tries = 0
    while tries < max_tries and node_and_port:
        time.sleep(1)
        node_and_port = cluster_tunnel.get_compute_node_and_port()
        tries += 1

    assert node_and_port == ''
