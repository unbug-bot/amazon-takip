#!/usr/bin/env python3
"""
GitHub Actions'ta çalışan yavaş Amazon.com.tr takibi.

Her çalıştırma küçük bir sayfa bütçesi (varsayılan 8) kullanır ve işleri sırayla yapar:
  kontrol : takipteki beden varyantlarının fiyat/stok kontrolü (en eskisi önce)
  arama   : arama sayfaları (Dior fiyatları buradan güncellenir; pantolon/gömlek için aday toplar)
  kesif   : aday ürün sayfasını açıp aranan bedeni arar
Ayrıştırma ve kayıt mantığı tracker.py'den gelir. Sonunda site/index.html üretilir.
"""
from __future__ import annotations

import html
import os
import random
import re
import time
from datetime import datetime, timedelta

import tracker as T
from tracker import log, tl

OTURUM = T.ROOT / "oturum.json"
SITE = T.ROOT / "site"

ONLINE_VARSAYILAN = {
    "sayfa_butcesi": 8,
    "bekleme_saniye": [20, 40],
    "kontrol_saat": 4,
    "arama_saat_bedensiz": 6,
    "arama_saat_bedenli": 24,
}

EK_SCHEMA = """
