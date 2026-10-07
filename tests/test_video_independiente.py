"""Video reciente sin consumir los cuadros del detector ni repetir congelados."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edge.sources import FrameInfo, LiveSource, SourceStatus
from edge.preview import PreviewPublisher


class VideoIndependiente(unittest.TestCase):
    def fuente(self):
        s = LiveSource.__new__(LiveSource)
        s._cond = threading.Condition()
        s._status = SourceStatus(connected=True)
        s._latest = FrameInfo(np.zeros((32, 48, 3), np.uint8), 1, time.time())
        s._last_delivered = 0
        return s

    def test_cursor_independiente_y_corte(self):
        s = self.fuente()
        self.assertEqual(s.ultimo_frame().index, 1)
        self.assertEqual(s._last_delivered, 0)
        self.assertIsNone(s.ultimo_frame(1))
        s._latest.ts -= 2
        self.assertIsNone(s.ultimo_frame())
        s._latest.ts = time.time()
        s._status.connected = False
        self.assertIsNone(s.ultimo_frame())

    def test_video_avanza_sin_ninguna_inferencia(self):
        s = self.fuente()
        enviados = []
        with patch.object(PreviewPublisher, '_subir', lambda self, jpg: enviados.append(jpg)):
            p = PreviewPublisher('http://127.0.0.1:1', 'test', 'prueba', fps=30)
            p._espectadores = 1
            try:
                p.conectar_fuente(s)
                for i in range(12):
                    with s._cond:
                        s._latest = FrameInfo(np.full((32,48,3), i * 15, np.uint8), i + 2, time.time())
                    time.sleep(.055)
                self.assertGreaterEqual(len(enviados), 8)
                self.assertEqual(s._last_delivered, 0)
            finally:
                p.cerrar()
            self.assertFalse(p._hilo_fuente.is_alive())


if __name__ == '__main__': unittest.main()
