#!/usr/bin/env python3
"""
Amazon.com.tr indirim ve stok takipçisi (macOS).

Komutlar:
  hazirla               Tarayıcıyı görünür açar: çerezleri kabul et, doğrulama çıkarsa çöz.
  calistir [--sadece ID] [--goster]
                        Takipleri tarar, değişiklikleri bildirir.
  rapor [--limit N]     Takip edilen ürünleri listeler.
  bildirim-test         Bildirim kanallarını dener.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "veri.db"
PROFILE_DIR = ROOT / "tarayici_profili"
LOG_PATH = ROOT / "takip.log"
LOCK_PATH = ROOT / ".calisiyor.lock"
BASE = "https://www.amazon.com.tr"

STOK_YOK_IFADELERI = ("mevcut değil", "stokta yok", "tükendi", "currently unavailable")


class DogrulamaGerekli(Exception):
    """Amazon CAPTCHA gösterdi."""


class LimitAsildi(Exception):
    """Bir çalıştırmadaki sayfa yükleme sınırı doldu."""


# ---------------------------------------------------------------- yardımcılar

def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("İ", "i").lower()).strip()


def parse_price(text: str | None) -> float | None:
    """'1.299,90 TL' -> 1299.9"""
    if not text:
        return None
    t = re.sub(r"[^\d,\.]", "", text)
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def tl(x: float | None) -> str:
    if x is None:
        return "-"
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + " TL"


def beden_uyar(etiket: str | None, kural: dict) -> bool:
    s = norm(etiket)
    if not s:
        return False
    if kural["tip"] == "bel_boy":
        # "34/34", "W34 L34", "34W x 34L", "Bel 34 Boy 34" -> [34, 34]
        sayilar = [int(n) for n in re.findall(r"\d+", s)]
        return sayilar == [int(kural["bel"]), int(kural["boy"])]
    if kural["tip"] == "harf":
        s2 = re.sub(r"\s*-\s*|\s+", "-", s)
        for a in kural["esdegerler"]:
            a2 = re.sub(r"\s*-\s*|\s+", "-", norm(a))
            # (?<![a-z0-9]) sayesinde 'xxl', 'xxxl' içinde eşleşmez
            if re.search(r"(?<![a-z0-9])" + re.escape(a2) + r"(?![a-z0-9])", s2):
                return True
        return False
    raise ValueError(f"Bilinmeyen beden tipi: {kural['tip']}")


BEL_BOY_DESENLERI = (
    r"\bW\s*(\d{2})\s*[xX×/\-]?\s*L\s*(\d{2})\b",       # W34 x L34, W34L34
    r"\b(\d{2})\s*W\s*[xX×/\-]?\s*(\d{2})\s*L\b",       # 34W x 34L
    r"(?<![\d.,/])(\d{2})\s*/\s*(\d{2})(?![\d/])",          # 34/34
)


def basliktan_beden(baslik: str, kural: dict) -> str | None:
    """Varyantı olmayan (tek bedenli) ilanlarda bedeni başlıktan bulur."""
    if not baslik:
        return None
    if kural["tip"] == "bel_boy":
        hedef = (int(kural["bel"]), int(kural["boy"]))
        for desen in BEL_BOY_DESENLERI:
            for m in re.finditer(desen, baslik, re.I):
                if (int(m.group(1)), int(m.group(2))) == hedef:
                    return m.group(0).strip()
        return None
    if kural["tip"] == "harf":
        s = re.sub(r"\s*-\s*|\s+", "-", norm(baslik))
        for a in kural["esdegerler"]:
            a2 = re.sub(r"\s*-\s*|\s+", "-", norm(a))
            if re.search(r"(?<![a-z0-9])" + re.escape(a2) + r"(?![a-z0-9])", s):
                return a.upper()
    return None


# ---- parfüm eşleştirme -------------------------------------------------
_HARF = "a-zçğıöşü"
_AKSAN = str.maketrans("âàáäéèêëîíìïôóòûúùñ’‘`´", "aaaaeeeeiiiiooouuun" + "'" * 4)
PARFUM_ORTAK_HARIC = (
    "muadil", "esintili", "esans", "esansı", "inspired", "benzeri", "alternatif", "decant",
    "bölünmüş", "dolum", "numune", "sample", "set", "seti", "kofre", "gift", "deo", "deodorant",
    "after shave", "aftershave", "shower", "duş", "body", "losyon", "balm", "stick", "sabun", "soap",
    "edt", "eau de toilette", "toilette", "cologne", "kolonya",
)


def _sade(s: str) -> str:
    """'La Nuit de L’Homme EDP100ml' -> 'la nuit de l homme edp100ml' (Türkçe harfler korunur)."""
    s = norm(s).translate(_AKSAN).replace("'", " ")
    return re.sub(rf"[^0-9{_HARF}]+", " ", s).strip()


def _kelime_var(sade: str, k: str) -> bool:
    return re.search(rf"(?<![{_HARF}]){re.escape(_sade(k))}(?![{_HARF}])", sade) is not None


def parfum_etiketi(baslik: str, kural: dict) -> str | None:
    """Başlık tam istenen parfümse 'EDP 100 ml' gibi bir etiket, değilse None döndürür.
    kural: gerekli (sıkıştırılmış başlıkta hepsi geçmeli), haric (ek kelimeler),
           konsantrasyon: 'serbest' | 'edp' (EDP yazmalı) | 'edp_parfum' (EDP ya da Parfum yazmalı)."""
    sade = _sade(baslik)
    bitisik = sade.replace(" ", "")
    if not all(_sade(g).replace(" ", "") in bitisik for g in kural["gerekli"]):
        return None
    if any(_kelime_var(sade, k) for k in (*PARFUM_ORTAK_HARIC, *kural.get("haric", []))):
        return None
    edp = _kelime_var(sade, "edp") or _kelime_var(sade, "eau de parfum")
    parfum = not edp and (_kelime_var(sade, "parfum") or _kelime_var(sade, "extrait"))
    if kural.get("konsantrasyon") == "edp_parfum" and not (edp or parfum):
        return None
    if kural.get("konsantrasyon") == "edp" and not edp:
        return None
    parca = []
    if "elixir" in bitisik:
        parca.append("Elixir")
    elif "leparfum" in bitisik:
        parca.append("Le Parfum")
    elif edp:
        parca.append("EDP")
    elif parfum:
        parca.append("Parfum")
    m = re.search(r"(\d{1,3})\s*ml(?![a-z])", sade)
    if m:
        parca.append(f"{int(m.group(1))} ml")
    if _kelime_var(sade, "tester"):
        parca.append("Tester")
    return " ".join(parca)


def baslik_uygun(baslik: str, takip: dict) -> bool:
    if takip.get("parfum"):
        return parfum_etiketi(baslik, takip["parfum"]) is not None
    t = norm(baslik)
    for grup in takip.get("baslikta_gerekli", []):
        if not any(norm(k) in t for k in grup):
            return False
    return not any(norm(k) in t for k in takip.get("baslikta_olmamali", []))


def marka_uygun(byline: str, takip: dict) -> bool:
    markalar = takip.get("markalar")
    if not markalar:
        return True
    b = norm(byline)
    if not b:
        return True  # sayfada marka satırı yok; arama marka filtresine güveniyoruz
    return any(re.search(r"(?<!\w)" + re.escape(norm(m)) + r"(?!\w)", b) for m in markalar)


def varyantlari_cikar(sayfa_html: str) -> dict[str, list[str]]:
    """Ürün sayfasındaki twister verisinden {çocuk_asin: [beden, renk, ...]}."""
    for kaynak in (sayfa_html, htmllib.unescape(sayfa_html)):
        m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})', kaynak, re.S)
        if not m:
            continue
        try:
            veri = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        return {a: ([d] if isinstance(d, str) else [str(x) for x in d]) for a, d in veri.items()}
    return {}


def varyant_uyar(degerler: list[str], kural: dict) -> bool:
    """Her boyut değeri tek tek, sonra ikili birleşimleri denenir.
    Böylece renk adındaki sayılar ('Indigo 501') bel/boy eşleşmesini bozmaz,
    bel ve boy ayrı boyutlarda olsa da ('34W', '34L') eşleşir."""
    if any(beden_uyar(d, kural) for d in degerler):
        return True
    for i in range(len(degerler)):
        for j in range(i + 1, len(degerler)):
            if beden_uyar(degerler[i] + " " + degerler[j], kural):
                return True
    return False


def parent_asin_bul(sayfa_html: str) -> str | None:
    m = re.search(r'"parentAsin"\s*:\s*"([A-Z0-9]{10})"', sayfa_html)
    return m.group(1) if m else None


# ---------------------------------------------------------------- sayfa JS'leri

ARAMA_JS = r"""
() => [...document.querySelectorAll('div[data-component-type="s-search-result"][data-asin]')]
  .map(el => {
    const t = s => { const e = el.querySelector(s); return e ? e.textContent.replace(/\s+/g,' ').trim() : ''; };
    return {
      asin: el.getAttribute('data-asin'),
      baslik: t('[data-cy="title-recipe"]') || t('h2'),
      fiyat: t('.a-price:not(.a-text-price) .a-offscreen'),
      liste: t('.a-price.a-text-price .a-offscreen'),
    };
  })
  .filter(x => /^[A-Z0-9]{10}$/.test(x.asin))
"""

URUN_JS = r"""
() => {
  const q = s => document.querySelector(s);
  const t = s => { const e = q(s); return e ? e.textContent.replace(/\s+/g,' ').trim() : ''; };
  const fiyatSec = [
    '#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen',
    '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen',
    '#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen',
    '.apexPriceToPay .a-offscreen',
    '#price_inside_buybox', '#priceblock_dealprice', '#priceblock_ourprice'
  ];
  let fiyat = '';
  for (const s of fiyatSec) { const v = t(s); if (v) { fiyat = v; break; } }
  return {
    baslik: t('#productTitle'),
    marka: t('#bylineInfo'),
    fiyat,
    liste: t('#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen')
        || t('#corePrice_feature_div .a-text-price .a-offscreen'),
    stok: t('#availability'),
    sepet: !!q('#add-to-cart-button'),
  };
}
"""

# Twister JSON bulunamazsa yedek: beden açılır listesi / satır içi beden kutuları
VARYANT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('#native_dropdown_selected_size_name option').forEach(o => {
    const p = (o.value || '').split(',');
    if (p.length > 1 && /^[A-Z0-9]{10}$/.test(p[1])) out.push({asin: p[1], etiket: o.textContent.trim()});
  });
  document.querySelectorAll('[id^="inline-twister-row-size_name"] li[data-asin], #tp-inline-twister-dim-values-container li[data-asin]')
    .forEach(li => {
      const a = li.getAttribute('data-asin');
      if (a && /^[A-Z0-9]{10}$/.test(a)) out.push({asin: a, etiket: (li.innerText || li.textContent).trim().split('\n')[0]});
    });
  return out;
}
"""


# ---------------------------------------------------------------- tarayıcı

class Tarayici:
    def __init__(self, ayarlar: dict, headless: bool):
        from playwright.sync_api import sync_playwright
        self.ayarlar = ayarlar
        self.istek = 0
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=headless, locale="tr-TR",
            timezone_id="Europe/Istanbul", viewport={"width": 1366, "height": 900},
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def git(self, url: str) -> None:
        if self.istek >= int(self.ayarlar["max_sayfa_yukleme"]):
            raise LimitAsildi()
        if self.istek:
            a, b = self.ayarlar["bekleme_saniye"]
            time.sleep(random.uniform(a, b))
        self.istek += 1
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if self.page.query_selector('form[action*="validateCaptcha"]') or "captcha" in self.page.url.lower():
            raise DogrulamaGerekli(url)
        try:
            btn = self.page.query_selector("#sp-cc-accept")
            if btn:
                btn.click(timeout=3000)
        except Exception:
            pass

    def kapat(self) -> None:
        # Ctrl+C tarayıcıyı da öldürür; kapatırken çıkan hata önemsiz.
        for f in (self.ctx.close, self.pw.stop):
            try:
                f()
            except Exception:
                pass


def urun_sayfasi(tr: Tarayici, asin: str) -> dict | None:
    tr.git(f"{BASE}/dp/{asin}?th=1&psc=1")
    b = tr.page.evaluate(URUN_JS)
    if not b["baslik"]:
        return None
    fiyat = parse_price(b["fiyat"])
    stok = norm(b["stok"])
    if any(k in stok for k in STOK_YOK_IFADELERI):
        stokta = False
    else:
        stokta = bool(b["sepet"]) and fiyat is not None
    return {"baslik": b["baslik"], "marka": b["marka"], "fiyat": fiyat,
            "liste": parse_price(b["liste"]), "stokta": stokta,
            "html": tr.page.content()}


def arama_sonuclari(tr: Tarayici, takip: dict) -> list[dict]:
    toplam: dict[str, dict] = {}
    marka_filtre = bool(takip.get("markalar"))
    for sorgu in takip["aramalar"]:
        for sayfa in range(1, int(takip.get("sayfa_sayisi", 3)) + 1):
            sonuc = _ara(tr, takip, sorgu, sayfa, marka_filtre)
            if not sonuc and sayfa == 1 and marka_filtre:
                log("  marka filtresiyle sonuç yok, filtresiz deneniyor")
                marka_filtre = False
                sonuc = _ara(tr, takip, sorgu, sayfa, False)
            if not sonuc:
                break
            for r in sonuc:
                toplam.setdefault(r["asin"], r)
    log(f"  arama: {len(toplam)} benzersiz sonuç")
    return list(toplam.values())


def _ara(tr: Tarayici, takip: dict, sorgu: str, sayfa: int, marka_filtre: bool) -> list[dict]:
    params = {"k": sorgu, "page": sayfa}
    if marka_filtre:
        params["rh"] = "p_89:" + "|".join(takip["markalar"])
    tr.git(f"{BASE}/s?" + urllib.parse.urlencode(params))
    return tr.page.evaluate(ARAMA_JS)


# ---------------------------------------------------------------- veritabanı

SCHEMA = """
CREATE TABLE IF NOT EXISTS urunler(
  asin TEXT PRIMARY KEY, takip_id TEXT, parent_asin TEXT, baslik TEXT, beden TEXT,
  son_fiyat REAL, en_dusuk REAL, liste_fiyati REAL, stokta INTEGER,
  ilk_gorulme TEXT, son_kontrol TEXT, aktif INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS gecmis(asin TEXT, ts TEXT, fiyat REAL, stokta INTEGER);
CREATE TABLE IF NOT EXISTS taranan(asin TEXT PRIMARY KEY, takip_id TEXT, ts TEXT, beden_var INTEGER);
CREATE INDEX IF NOT EXISTS gecmis_asin ON gecmis(asin);
"""


def db_baglan() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def kaydet(con, takip, ayarlar, olaylar, *, asin, parent, baslik, beden,
           fiyat, liste, stokta, yeni_beden=False) -> None:
    simdi = datetime.now().isoformat(timespec="seconds")
    eski = con.execute("SELECT * FROM urunler WHERE asin=?", (asin,)).fetchone()
    hedef = takip.get("hedef_fiyat")
    esik = float(ayarlar["min_indirim_yuzde"])
    nedenler: list[str] = []

    if eski is None:
        if stokta and yeni_beden:
            nedenler.append("STOĞA GİRDİ (yeni beden)")
        if stokta and hedef and fiyat is not None and fiyat <= hedef:
            nedenler.append(f"HEDEF FİYAT ≤ {tl(hedef)}")
        if ayarlar.get("yeni_urun_bildir") and stokta and not nedenler:
            nedenler.append("YENİ ÜRÜN")
        con.execute(
            "INSERT INTO urunler(asin,takip_id,parent_asin,baslik,beden,son_fiyat,en_dusuk,"
            "liste_fiyati,stokta,ilk_gorulme,son_kontrol,aktif) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (asin, takip["id"], parent, baslik, beden, fiyat,
             fiyat if (stokta and fiyat is not None) else None,
             liste, int(stokta), simdi, simdi))
    else:
        eski_fiyat, eski_min, eski_stok = eski["son_fiyat"], eski["en_dusuk"], bool(eski["stokta"])
        if takip.get("beden") and stokta and not eski_stok:
            nedenler.append("STOĞA GİRDİ")
        if stokta and fiyat is not None:
            if eski_fiyat and fiyat <= eski_fiyat * (1 - esik / 100):
                nedenler.append(f"İNDİRİM %{(1 - fiyat / eski_fiyat) * 100:.0f} "
                                f"({tl(eski_fiyat)} → {tl(fiyat)})")
            elif eski_min and fiyat < eski_min * 0.99:
                nedenler.append(f"EN DÜŞÜK FİYAT (önceki en düşük {tl(eski_min)})")
            if hedef and fiyat <= hedef and not (eski_stok and eski_fiyat is not None and eski_fiyat <= hedef):
                nedenler.append(f"HEDEF FİYAT ≤ {tl(hedef)}")
        yeni_min = eski_min
        if stokta and fiyat is not None:
            yeni_min = fiyat if eski_min is None else min(eski_min, fiyat)
        con.execute(
            "UPDATE urunler SET baslik=COALESCE(NULLIF(?,''),baslik), beden=COALESCE(?,beden),"
            " son_fiyat=COALESCE(?,son_fiyat), en_dusuk=?, liste_fiyati=COALESCE(?,liste_fiyati),"
            " stokta=?, son_kontrol=? WHERE asin=?",
            (baslik, beden, fiyat, yeni_min, liste, int(stokta), simdi, asin))

    con.execute("INSERT INTO gecmis VALUES(?,?,?,?)", (asin, simdi, fiyat, int(stokta)))
    con.commit()
    if nedenler:
        url = f"{BASE}/dp/{asin}" + ("?th=1&psc=1" if beden else "")
        olaylar.append({"takip": takip["ad"], "baslik": baslik, "beden": beden,
                        "fiyat": fiyat, "url": url, "nedenler": nedenler})
        log(f"  >> {', '.join(nedenler)} | {baslik[:60]} {beden or ''} | {tl(fiyat)}")


def taranma_kaydi(con, asin, takip_id, beden_var) -> None:
    con.execute("INSERT OR REPLACE INTO taranan VALUES(?,?,?,?)",
                (asin, takip_id, datetime.now().isoformat(timespec="seconds"), int(beden_var)))
    con.commit()


def yakin_zamanda_tarandi(con, asin, gun) -> bool:
    r = con.execute("SELECT ts FROM taranan WHERE asin=?", (asin,)).fetchone()
    return bool(r) and datetime.fromisoformat(r["ts"]) > datetime.now() - timedelta(days=gun)


# ---------------------------------------------------------------- takip türleri

def filtreyi_uygula(con, takip) -> None:
    """config'deki başlık filtreleri değişince artık uymayan kayıtları pasifleştirir."""
    satirlar = con.execute("SELECT asin, baslik FROM urunler WHERE takip_id=? AND aktif=1",
                           (takip["id"],)).fetchall()
    dis = [s["asin"] for s in satirlar if not baslik_uygun(s["baslik"] or "", takip)]
    if dis:
        con.executemany("UPDATE urunler SET aktif=0 WHERE asin=?", [(a,) for a in dis])
        con.commit()
        log(f"  filtreye artık uymayan {len(dis)} kayıt takipten çıkarıldı")

def guvenli(fn, *a, **k):
    """Tek ürün hatası tüm çalıştırmayı durdurmasın."""
    try:
        return fn(*a, **k)
    except (DogrulamaGerekli, LimitAsildi):
        raise
    except Exception as e:
        log(f"  ürün hatası: {e!r}")
        return None


def bedensiz_takip(tr, con, takip, ayarlar, olaylar) -> None:
    """Arama sonucundaki fiyatla takip (ör. Dior parfüm). Ürün sayfası açılmaz."""
    for r in arama_sonuclari(tr, takip):
        if not baslik_uygun(r["baslik"], takip):
            continue
        if con.execute("SELECT 1 FROM urunler WHERE asin=? AND aktif=0", (r["asin"],)).fetchone():
            continue
        fiyat = parse_price(r["fiyat"])
        if fiyat is None:
            continue
        kaydet(con, takip, ayarlar, olaylar, asin=r["asin"], parent=None, baslik=r["baslik"],
               beden=None, fiyat=fiyat, liste=parse_price(r["liste"]), stokta=True)


def bedenli_takip(tr, con, takip, ayarlar, olaylar) -> None:
    kural = takip["beden"]

    # 1) Bilinen beden varyantlarını güncelle
    satirlar = con.execute("SELECT asin,parent_asin,beden FROM urunler WHERE takip_id=? AND aktif=1",
                           (takip["id"],)).fetchall()
    log(f"  takipte {len(satirlar)} varyant")
    for i, s in enumerate(satirlar, 1):
        b = guvenli(urun_sayfasi, tr, s["asin"])
        if b:
            log(f"  kontrol {i}/{len(satirlar)}: {b['baslik'][:45]} [{s['beden']}] "
                f"{tl(b['fiyat'])} {'stokta' if b['stokta'] else 'stok yok'}")
            kaydet(con, takip, ayarlar, olaylar, asin=s["asin"], parent=s["parent_asin"],
                   baslik=b["baslik"], beden=s["beden"], fiyat=b["fiyat"],
                   liste=b["liste"], stokta=b["stokta"])

    # 2) Yeni ürün keşfi
    gun = int(takip.get("yeniden_tarama_gun", 7))
    limit = int(takip.get("yeni_urun_limiti", 40))
    acilan = 0
    for r in arama_sonuclari(tr, takip):
        if acilan >= limit:
            log(f"  keşif limiti ({limit}) doldu, kalanlar sonraki çalıştırmada")
            break
        if not baslik_uygun(r["baslik"], takip) or yakin_zamanda_tarandi(con, r["asin"], gun):
            continue
        acilan += 1
        log(f"  keşif {acilan}/{limit}: {r['baslik'][:60]}")
        guvenli(_kesfet, tr, con, takip, ayarlar, olaylar, r["asin"], kural, gun)


def _kesfet(tr, con, takip, ayarlar, olaylar, asin, kural, gun) -> None:
    b = urun_sayfasi(tr, asin)
    if not b:
        taranma_kaydi(con, asin, takip["id"], False)
        return
    sayfa = b["html"]
    parent = parent_asin_bul(sayfa) or asin
    onceden = con.execute("SELECT 1 FROM taranan WHERE asin=?", (parent,)).fetchone() is not None

    if parent != asin and yakin_zamanda_tarandi(con, parent, gun):
        taranma_kaydi(con, asin, takip["id"], False)
        return
    if not marka_uygun(b["marka"], takip):
        taranma_kaydi(con, asin, takip["id"], False)
        taranma_kaydi(con, parent, takip["id"], False)
        return

    varyantlar = varyantlari_cikar(sayfa)
    if not varyantlar:
        varyantlar = {v["asin"]: [v["etiket"]] for v in tr.page.evaluate(VARYANT_JS)}
    eslesen = {a: " ".join(d) for a, d in varyantlar.items() if varyant_uyar(d, kural)}
    kaynak = ""
    if not varyantlar:
        # Tek bedenli ilan: beden yalnızca başlıkta yazıyor olabilir ("... W34 x L34")
        etiket = basliktan_beden(b["baslik"], kural)
        if etiket:
            eslesen = {asin: f"{etiket} (başlıktan)"}
            kaynak = " (başlıktan)"
    log(f"     {len(varyantlar)} varyant, aranan beden: {len(eslesen)}{kaynak}")
    taranma_kaydi(con, asin, takip["id"], bool(eslesen))
    taranma_kaydi(con, parent, takip["id"], bool(eslesen))
    con.commit()

    for cocuk, etiket in eslesen.items():
        if con.execute("SELECT 1 FROM urunler WHERE asin=?", (cocuk,)).fetchone():
            continue
        cb = b if cocuk == asin else urun_sayfasi(tr, cocuk)
        if not cb:
            continue
        kaydet(con, takip, ayarlar, olaylar, asin=cocuk, parent=parent, baslik=cb["baslik"],
               beden=etiket, fiyat=cb["fiyat"], liste=cb["liste"], stokta=cb["stokta"],
               yeni_beden=onceden)


# ---------------------------------------------------------------- bildirim

def bildir(baslik: str, mesaj: str, url: str) -> None:
    tn = next((p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
               if Path(p).exists()), None) or shutil.which("terminal-notifier")
    try:
        if tn:
            subprocess.run([tn, "-title", baslik, "-message", mesaj, "-open", url,
                            "-sound", "default"], check=False, timeout=15)
        else:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display notification "{esc(mesaj)}" with title "{esc(baslik)}" sound name "Glass"'],
                           check=False, timeout=15)
    except Exception as e:
        log(f"bildirim hatası: {e!r}")


def telegram(metin: str, ayarlar: dict) -> None:
    t = ayarlar.get("telegram") or {}
    if not t.get("bot_token") or not t.get("chat_id"):
        return
    for i in range(0, len(metin), 3900):
        veri = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": metin[i:i + 3900],
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage",
                                   data=veri, timeout=20)
        except Exception as e:
            log(f"telegram hatası: {e!r}")


def olaylari_gonder(olaylar: list[dict], ayarlar: dict) -> None:
    if not olaylar:
        log("Bildirilecek değişiklik yok.")
        return
    satirlar = []
    for o in olaylar:
        beden = f" [{o['beden']}]" if o["beden"] else ""
        satirlar.append(f"{', '.join(o['nedenler'])}\n{o['baslik'][:90]}{beden}\n{tl(o['fiyat'])}\n{o['url']}")
    if len(olaylar) <= 3:
        for o in olaylar:
            bildir(f"{o['takip']}: {o['nedenler'][0]}", f"{tl(o['fiyat'])} · {o['baslik'][:80]}", o["url"])
    else:
        bildir(f"Amazon: {len(olaylar)} değişiklik", "Ayrıntılar Telegram'da / tracker.py rapor", BASE)
    telegram("\n\n".join(satirlar), ayarlar)


# ---------------------------------------------------------------- komutlar

def ayarlari_yukle() -> dict:
    import yaml
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    a = cfg.setdefault("ayarlar", {})
    a.setdefault("headless", True)
    a.setdefault("bekleme_saniye", [4, 8])
    a.setdefault("min_indirim_yuzde", 5)
    a.setdefault("max_sayfa_yukleme", 400)
    a.setdefault("yeni_urun_bildir", False)
    a.setdefault("telegram", {})
    return cfg


def kilit_al() -> bool:
    if LOCK_PATH.exists():
        try:
            os.kill(int(LOCK_PATH.read_text().strip()), 0)
            return False
        except (ValueError, ProcessLookupError):
            pass
        except PermissionError:
            return False
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def cmd_calistir(args) -> None:
    cfg = ayarlari_yukle()
    a = cfg["ayarlar"]
    if not kilit_al():
        log("Önceki çalıştırma sürüyor, çıkılıyor.")
        return
    con = db_baglan()
    olaylar: list[dict] = []
    tr = None
    try:
        tr = Tarayici(a, headless=a["headless"] and not args.goster)
        for takip in cfg["takipler"]:
            if (args.sadece and takip["id"] != args.sadece) or not takip.get("aktif", True):
                continue
            log(f"== {takip['ad']} ==")
            filtreyi_uygula(con, takip)
            try:
                (bedenli_takip if takip.get("beden") else bedensiz_takip)(tr, con, takip, a, olaylar)
            except (DogrulamaGerekli, LimitAsildi):
                raise
            except Exception as e:
                log(f"  takip hatası ({takip['id']}): {e!r}")
    except DogrulamaGerekli as e:
        log(f"CAPTCHA: {e}. 'tracker.py hazirla' ile çöz.")
        bildir("Amazon doğrulama istedi", "Terminalde: tracker.py hazirla", BASE)
        telegram("Amazon doğrulama (CAPTCHA) istedi. Mac'te: tracker.py hazirla", a)
    except LimitAsildi:
        log(f"Sayfa yükleme sınırı ({a['max_sayfa_yukleme']}) doldu; kalanlar sonraki çalıştırmada.")
    except KeyboardInterrupt:
        log("Durduruldu (Ctrl+C). Buraya kadar bulunanlar kaydedildi.")
    finally:
        try:
            con.commit()
            con.close()
        finally:
            LOCK_PATH.unlink(missing_ok=True)
            if tr:
                log(f"Toplam sayfa yüklemesi: {tr.istek}")
                tr.kapat()
    olaylari_gonder(olaylar, a)


def cmd_hazirla(args) -> None:
    a = ayarlari_yukle()["ayarlar"]
    tr = Tarayici(a, headless=False)
    tr.page.goto(BASE)
    input("\nAçılan pencerede: çerezleri kabul et, doğrulama çıkarsa çöz, istersen teslimat\n"
          "adresini (il/ilçe) seç. Bitince buraya dönüp Enter'a bas... ")
    tr.kapat()
    print("Profil kaydedildi.")


def cmd_rapor(args) -> None:
    cfg = ayarlari_yukle()
    con = db_baglan()
    for takip in cfg["takipler"]:
        filtreyi_uygula(con, takip)
        satirlar = con.execute(
            "SELECT * FROM urunler WHERE takip_id=? AND aktif=1 ORDER BY stokta DESC, son_fiyat",
            (takip["id"],)).fetchall()
        stokta = sum(1 for s in satirlar if s["stokta"])
        print(f"\n## {takip['ad']}: {len(satirlar)} ürün, {stokta} stokta")
        for s in satirlar[: args.limit]:
            durum = "STOKTA " if s["stokta"] else "yok    "
            beden = f" [{s['beden']}]" if s["beden"] else ""
            print(f"  {durum} {tl(s['son_fiyat']):>13}  (en düşük {tl(s['en_dusuk'])})  "
                  f"{(s['baslik'] or '')[:55]}{beden}")
            print(f"          {BASE}/dp/{s['asin']}")


def cmd_bildirim_test(args) -> None:
    a = ayarlari_yukle()["ayarlar"]
    bildir("Amazon takip", "Test bildirimi", BASE)
    telegram("Amazon takip: test mesajı", a)
    print("Gönderildi (Telegram ayarlıysa oraya da).")


def main() -> None:
    p = argparse.ArgumentParser(description="Amazon.com.tr indirim/stok takipçisi")
    alt = p.add_subparsers(dest="komut", required=True)
    c = alt.add_parser("calistir")
    c.add_argument("--sadece", help="yalnızca bu takip id'sini çalıştır")
    c.add_argument("--goster", action="store_true", help="tarayıcıyı görünür aç")
    c.set_defaults(fn=cmd_calistir)
    alt.add_parser("hazirla").set_defaults(fn=cmd_hazirla)
    r = alt.add_parser("rapor")
    r.add_argument("--limit", type=int, default=30)
    r.set_defaults(fn=cmd_rapor)
    alt.add_parser("bildirim-test").set_defaults(fn=cmd_bildirim_test)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
