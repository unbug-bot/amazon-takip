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
import random
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
CREATE TABLE IF NOT EXISTS durum(anahtar TEXT PRIMARY KEY, deger TEXT);
CREATE TABLE IF NOT EXISTS adaylar(asin TEXT PRIMARY KEY, takip_id TEXT, baslik TEXT, eklenme TEXT);
CREATE TABLE IF NOT EXISTS calismalar(ts TEXT, sayfa INTEGER, captcha INTEGER, sonuc TEXT);
"""


def simdi_s() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dget(con, k):
    r = con.execute("SELECT deger FROM durum WHERE anahtar=?", (k,)).fetchone()
    return r[0] if r else None


def dset(con, k, v) -> None:
    con.execute("INSERT OR REPLACE INTO durum VALUES(?,?)", (k, str(v)))
    con.commit()


def eski_mi(ts, saat) -> bool:
    return ts is None or datetime.fromisoformat(ts) < datetime.now() - timedelta(hours=saat)


# ---------------------------------------------------------------- tarayıcı

class OnlineTarayici:
    """tracker.Tarayici ile aynı arayüz (git, page). Oturum çerezleri dosyada saklanır."""

    def __init__(self, o: dict):
        from playwright.sync_api import sync_playwright
        self.o = o
        self.butce = int(o["sayfa_butcesi"])
        self.istek = 0
        self.captcha = 0
        self.pw = sync_playwright().start()
        self.tarayici = self.pw.chromium.launch(headless=True)
        kw = dict(locale="tr-TR", timezone_id="Europe/Istanbul",
                  viewport={"width": 1366, "height": 900})
        if OTURUM.exists():
            kw["storage_state"] = str(OTURUM)
        self.ctx = self.tarayici.new_context(**kw)
        self.page = self.ctx.new_page()

    def kalan(self) -> int:
        return self.butce - self.istek

    def _yukle(self, url: str) -> None:
        if self.istek >= self.butce + 6:  # keşif birkaç sayfa taşabilir, fazlası değil
            raise T.LimitAsildi()
        if self.istek:
            time.sleep(random.uniform(*self.o["bekleme_saniye"]))
        self.istek += 1
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def _captcha_mi(self) -> bool:
        return bool(self.page.query_selector('form[action*="validateCaptcha"]')) \
            or "captcha" in self.page.url.lower()

    def git(self, url: str) -> None:
        self._yukle(url)
        if self._captcha_mi():
            self.captcha += 1
            log("  CAPTCHA; ana sayfa açılıp bir kez yeniden denenecek")
            self._yukle(T.BASE)
            self._yukle(url)
            if self._captcha_mi():
                self.captcha += 1
                raise T.DogrulamaGerekli(url)
        try:
            btn = self.page.query_selector("#sp-cc-accept")
            if btn:
                btn.click(timeout=3000)
        except Exception:
            pass

    def kapat(self) -> None:
        for f in (lambda: self.ctx.storage_state(path=str(OTURUM)),
                  self.ctx.close, self.tarayici.close, self.pw.stop):
            try:
                f()
            except Exception:
                pass


# ---------------------------------------------------------------- görevler

def takip_bul(cfg, tid):
    return next((t for t in cfg["takipler"] if t["id"] == tid and t.get("aktif", True)), None)


def gorev_kontrol(tr, con, cfg, a, o, olaylar) -> bool:
    ids = [t["id"] for t in cfg["takipler"] if t.get("beden") and t.get("aktif", True)]
    if not ids:
        return False
    sinir = (datetime.now() - timedelta(hours=o["kontrol_saat"])).isoformat(timespec="seconds")
    r = con.execute(
        f"SELECT asin,takip_id,parent_asin,beden FROM urunler WHERE aktif=1 AND takip_id IN "
        f"({','.join('?' * len(ids))}) AND (son_kontrol IS NULL OR son_kontrol < ?) "
        f"ORDER BY son_kontrol LIMIT 1", (*ids, sinir)).fetchone()
    if not r:
        return False
    takip = takip_bul(cfg, r["takip_id"])
    b = T.guvenli(T.urun_sayfasi, tr, r["asin"])
    if b:
        dset(con, f"hata:{r['asin']}", 0)
        T.kaydet(con, takip, a, olaylar, asin=r["asin"], parent=r["parent_asin"], baslik=b["baslik"],
                 beden=r["beden"], fiyat=b["fiyat"], liste=b["liste"], stokta=b["stokta"])
        log(f"  kontrol: {b['baslik'][:45]} [{r['beden']}] {tl(b['fiyat'])} "
            f"{'stokta' if b['stokta'] else 'stok yok'}")
    else:
        hata = int(dget(con, f"hata:{r['asin']}") or 0) + 1
        dset(con, f"hata:{r['asin']}", hata)
        con.execute("UPDATE urunler SET son_kontrol=? WHERE asin=?", (simdi_s(), r["asin"]))
        if hata >= 5:
            con.execute("UPDATE urunler SET aktif=0 WHERE asin=?", (r["asin"],))
            log(f"  {r['asin']} 5 kez okunamadı, takipten çıkarıldı")
        con.commit()
    return True


def gorev_arama(tr, con, cfg, a, o, olaylar) -> bool:
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        saat = o["arama_saat_bedenli"] if t.get("beden") else o["arama_saat_bedensiz"]
        n = int(t.get("sayfa_sayisi", 3))
        for sorgu in t["aramalar"]:
            for s in range(1, n + 1):
                k = f"arama:{t['id']}:{sorgu}:{s}"
                if eski_mi(dget(con, k), saat):
                    _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k)
                    return True
    return False


def _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k) -> None:
    marka = bool(t.get("markalar")) and dget(con, f"markasiz:{t['id']}") is None
    sonuc = T._ara(tr, t, sorgu, s, marka)
    if not sonuc and s == 1 and marka:
        log("  marka filtresiyle sonuç yok; bu takipte filtresiz aranacak")
        dset(con, f"markasiz:{t['id']}", 1)
        sonuc = T._ara(tr, t, sorgu, s, False)
    dset(con, k, simdi_s())
    if not sonuc:
        for s2 in range(s + 1, n + 1):  # sonraki sayfalar da boştur
            dset(con, f"arama:{t['id']}:{sorgu}:{s2}", simdi_s())
        log(f"  arama '{sorgu}' s{s}: sonuç yok")
        return
    if t.get("beden"):
        gun = int(t.get("yeniden_tarama_gun", 7))
        eklenen = 0
        for r in sonuc:
            if T.baslik_uygun(r["baslik"], t) and not T.yakin_zamanda_tarandi(con, r["asin"], gun):
                eklenen += con.execute("INSERT OR IGNORE INTO adaylar VALUES(?,?,?,?)",
                                       (r["asin"], t["id"], r["baslik"], simdi_s())).rowcount
        con.commit()
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {eklenen} yeni aday")
    else:
        kayit = 0
        for r in sonuc:
            if not T.baslik_uygun(r["baslik"], t):
                continue
            if con.execute("SELECT 1 FROM urunler WHERE asin=? AND aktif=0", (r["asin"],)).fetchone():
                continue
            fiyat = T.parse_price(r["fiyat"])
            if fiyat is None:
                continue
            T.kaydet(con, t, a, olaylar, asin=r["asin"], parent=None, baslik=r["baslik"], beden=None,
                     fiyat=fiyat, liste=T.parse_price(r["liste"]), stokta=True)
            kayit += 1
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {kayit} fiyat kaydedildi")


def gorev_kesif(tr, con, cfg, a, o, olaylar) -> bool:
    while True:
        r = con.execute("SELECT asin,takip_id,baslik FROM adaylar ORDER BY eklenme LIMIT 1").fetchone()
        if not r:
            return False
        con.execute("DELETE FROM adaylar WHERE asin=?", (r["asin"],))
        con.commit()
        t = takip_bul(cfg, r["takip_id"])
        if t and t.get("beden") and not T.yakin_zamanda_tarandi(
                con, r["asin"], int(t.get("yeniden_tarama_gun", 7))):
            break
    log(f"  keşif: {r['baslik'][:60]}")
    T.guvenli(T._kesfet, tr, con, t, a, olaylar, r["asin"], t["beden"],
              int(t.get("yeniden_tarama_gun", 7)))
    return True


GOREVLER = {"kontrol": gorev_kontrol, "arama": gorev_arama, "kesif": gorev_kesif}
SIRA = ["kontrol", "kontrol", "arama", "kesif"]


# ---------------------------------------------------------------- web sayfası (sade)

def site_uret(con, cfg) -> None:
    SITE.mkdir(exist_ok=True)
    e = html.escape
    son = con.execute("SELECT * FROM calismalar ORDER BY rowid DESC LIMIT 1").fetchone()
    parca = [f"<h1>Amazon fiyat takibi</h1><p>Son güncelleme: {datetime.now():%d.%m.%Y %H:%M}</p>"]
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        rows = con.execute("SELECT * FROM urunler WHERE takip_id=? AND aktif=1 "
                           "ORDER BY stokta DESC, son_fiyat", (t["id"],)).fetchall()
        parca.append(f"<h2>{e(t['ad'])}</h2>")
        if not rows:
            parca.append("<p>Henüz ürün yok.</p>")
            continue
        parca.append("<ul>")
        for r in rows:
            url = f"{T.BASE}/dp/{r['asin']}" + ("?th=1&psc=1" if r["beden"] else "")
            fiyat = tl(r["son_fiyat"]) if r["stokta"] else "stokta yok"
            ek = f" [{e(r['beden'])}]" if r["beden"] else ""
            dip = (f" <small>(en düşük {tl(r['en_dusuk'])})</small>"
                   if r["stokta"] and r["en_dusuk"] and r["son_fiyat"] and r["en_dusuk"] < r["son_fiyat"] else "")
            parca.append(f'<li><b>{fiyat}</b>{dip} <a href="{e(url)}">{e(r["baslik"] or r["asin"])}</a>{ek}</li>')
        parca.append("</ul>")
    if son:
        parca.append(f"<p><small>Son çalışma: {e(son['ts'][:16].replace('T', ' '))}, "
                     f"{son['sayfa']} sayfa, sonuç: {e(son['sonuc'])}</small></p>")
    sayfa = ('<!doctype html><html lang="tr"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<meta name="robots" content="noindex"><title>Amazon fiyat takibi</title>'
             '<style>:root{color-scheme:light dark}body{font-family:-apple-system,sans-serif;'
             'max-width:800px;margin:0 auto;padding:12px;line-height:1.5}li{margin:6px 0}</style>'
             f'</head><body>{"".join(parca)}</body></html>')
    (SITE / "index.html").write_text(sayfa, encoding="utf-8")


# ---------------------------------------------------------------- ana akış

def main() -> None:
    cfg = T.ayarlari_yukle()
    a = cfg["ayarlar"]
    o = {**ONLINE_VARSAYILAN, **(cfg.get("online") or {})}
    con = T.db_baglan()
    con.executescript(EK_SCHEMA)
    for t in cfg["takipler"]:
        T.filtreyi_uygula(con, t)

    olaylar: list = []
    sonuc = "tamam"
    tr = None
    try:
        tr = OnlineTarayici(o)
        while tr.kalan() > 0:
            yapildi = False
            for tip in SIRA:
                if tr.kalan() <= 0:
                    break
                yapildi = GOREVLER[tip](tr, con, cfg, a, o, olaylar) or yapildi
            if not yapildi:
                log("Yapılacak iş kalmadı.")
                break
    except T.DogrulamaGerekli as ex:
        sonuc = "captcha"
        log(f"CAPTCHA yeniden denemede de çıktı ({ex}); bu çalışma burada bitti.")
    except T.LimitAsildi:
        sonuc = "limit"
    except Exception as ex:
        sonuc = "hata"
        log(f"HATA: {ex!r}")
    finally:
        if tr:
            tr.kapat()
        con.execute("INSERT INTO calismalar VALUES(?,?,?,?)",
                    (simdi_s(), tr.istek if tr else 0, tr.captcha if tr else 0, sonuc))
        con.commit()
        log(f"Bitti: {tr.istek if tr else 0} sayfa, {tr.captcha if tr else 0} CAPTCHA, sonuç: {sonuc}")
    site_uret(con, cfg)
    con.close()


if __name__ == "__main__":
    main()
