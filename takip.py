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
