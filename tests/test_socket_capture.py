import pytest
import os
import sys
import numpy as np
from PIL import Image

sys.path.append(os.path.dirname(os.path.abspath(os.path.join(__file__, '..'))))
from monitor import SanguineHealthMonitor

def test_grab_from_socket(mocker):
    # Instantiate first so pynput thread gets a real socket
    monitor = SanguineHealthMonitor()
    
    # Mock the socket path check to always return True
    mocker.patch('os.path.exists', return_value=True)
    mocker.patch.object(monitor, 'get_socket_path', return_value='/fake/path.sock')
    
    # We will use a context manager to only mock socket during the grab call
    with mocker.mock_module.patch('socket.socket') as mock_socket:
        mock_client = mock_socket.return_value
        
        # We simulate a stream of bytes
        recv_buffer = bytearray(b"OK 10 10\n" + b"\x00" * 300)
        def mock_recv(size):
            nonlocal recv_buffer
            chunk = recv_buffer[:size]
            recv_buffer = recv_buffer[size:]
            return bytes(chunk)
            
        mock_client.recv.side_effect = mock_recv
        
        img = monitor.grab_from_socket(0, 0, 10, 10)
    
    assert img is not None
    assert isinstance(img, Image.Image)
    assert img.size == (10, 10)
    
    # Verify the socket was called correctly
    assert isinstance(img, Image.Image)
    assert img.size == (10, 10)
    
    # Verify the socket was called correctly
    mock_client.connect.assert_called_with('/fake/path.sock')
    mock_client.sendall.assert_called_with(b"0 0 10 10\n")
    mock_client.close.assert_called_once()
