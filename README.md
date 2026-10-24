# cmedia

Päätteessä toimiva tiedostoselain kuville, videoille ja äänitiedostoille. Chafa piirtää kuvat; FFmpeg purkaa videon kuva- ja äänivirrat. `pygame.mixer` toistaa videon äänen ja `pygame.mixer.music` äänitiedostot.

## Käyttö

Asenna `chafa`, `ffmpeg` ja `ffprobe` järjestelmään. Käynnistä lähdekoodihakemistossa:

```sh
uv run cmedia [hakemisto-tai-mediatiedosto]
```

Vaihtoehtoisesti: `python cmedia.py [polku]`.

Selaimessa nuolinäppäimet tai `j`/`k` valitsevat, PageUp ja PageDown siirtyvät näkyvän sivun verran, ja Home sekä End vievät listan alkuun ja loppuun. Enter avaa ja vasen nuoli tai Backspace vie ylähakemistoon. `/` avaa hakemistopolun syöttökentän; suhteellinen polku tulkitaan nykyisestä hakemistosta. Oikealla näkyy valitun kuvan tai videon esikatselu ilman ääntä sekä tiedoston metatiedot (`ffprobe`). `q` lopettaa. Median katselusta tai kuuntelusta pääsee takaisin Escillä tai `q`:lla. Videon ja äänen saa tauolle välilyönnillä. Äänitiedoston loputtua palataan selaimeen.

Tiedostolistassa videot ovat kirkkaanpunaisia, kuvat kirkkaanvihreitä ja äänitiedostot kirkkaankeltaisia.

`*` vaihtaa kuvien ja videoiden toistotilaa päätteen ja Pygamen erillisen ikkunan välillä. Selaimen oikeassa yläkulmassa näkyy `output: chafa` valkoisena tai `output: pygame` sinisenä. Ikkunatilassa kuva tai video avautuu lähteen natiiviresoluutiolla; `f` vaihtaa 33 %:n koon ja natiivikoon välillä. FFmpeg purkaa videoruudut ja Pygame piirtää ne ilman Chafan päätetulostetta. Ikkunan voi sulkea Escillä, `q`:lla tai sulkemispainikkeella; videon välilyönti pysäyttää ja jatkaa toistoa. Suoraan mediatiedostolla käynnistettäessä ikkunatilan saa päälle valinnalla `--media-window` (myös vanha `--video-window` toimii).

`--format auto` käyttää Kittyä Kitty-päätteessä ja Sixel-grafiikkaa tunnetuissa Sixel-päätteissä. Muissa päätteissä käytetään merkkigrafiikkaa. Grafiikkamuodon voi valita myös suoraan: `--format kitty`, `--format sixels` tai `--format symbols`.
