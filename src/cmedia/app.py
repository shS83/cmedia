"""Terminal file browser and media viewer using chafa and ffmpeg."""
from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime
import io
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
import zlib
from pathlib import Path
import platform


IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp', '.avif'}
VIDEOS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg'}
AUDIO = {'.wav', '.mp3', '.ogg', '.flac', '.opus', '.wv', '.mod', '.mid', '.midi'}
ESC = '\x1b'


def media_kind(path: Path) -> str | None:
    if path.suffix.lower() in IMAGES:
        return 'image'
    if path.suffix.lower() in VIDEOS:
        return 'video'
    if path.suffix.lower() in AUDIO:
        return 'audio'
    return None


def entries(directory: Path) -> list[Path]:
    try:
        paths = [p for p in directory.iterdir() if p.is_dir() or (p.is_file() and media_kind(p))]
    except OSError as exc:
        raise RuntimeError(f'Hakemistoa ei voi lukea: {exc}') from exc
    return sorted(paths, key=lambda p: (not p.is_dir(), p.name.casefold()))

def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False

    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True

    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def is_windows_terminal() -> bool:
    return bool(os.environ.get("WT_SESSION"))


def chafa_format() -> str:
    if is_windows_terminal():
        return "sixels"

    if is_wsl():
        return "sixels"

    if os.environ.get("TERM", "").startswith("xterm-kitty"):
        return "kitty"

    return "symbols"

def graphics_format(choice: str) -> str:
    if choice != 'auto':
        return choice
    term = os.environ.get('TERM', '').lower()
    if os.environ.get('KITTY_WINDOW_ID') or 'kitty' in term:
        return 'kitty'
    if 'sixel' in term or 'mlterm' in term or term.startswith('foot'):
        return 'sixels'

    return chafa_format()


def safe_text(value: object) -> str:
    return ''.join(char if char.isprintable() else ' ' for char in str(value))


class Terminal:
    def __enter__(self) -> Terminal:
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.write(f'{ESC}[?1049h{ESC}[?25l')
        return self

    def __exit__(self, *_: object) -> None:
        self.write(f'{ESC}[?1049l{ESC}[?25h')
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    @staticmethod
    def write(value: str | bytes) -> None:
        sys.stdout.buffer.write(value.encode() if isinstance(value, str) else value)
        sys.stdout.buffer.flush()

    @staticmethod
    def size() -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines

    def park(self) -> None:
        width, height = self.size()
        self.write(f'{ESC}[?25l{ESC}[{height};{width}H')

    @staticmethod
    def image_bytes(data: bytes, row: int, column: int) -> bytes:
        """Place Chafa output at a cell, omitting its cursor-show command."""
        data = data.replace(b'\x1b[?25h', b'')
        output = bytearray()
        for offset, line in enumerate(data.split(b'\n')):
            if line:
                output.extend(f'{ESC}[{row + offset};{column}H'.encode())
                output.extend(line)
        return bytes(output)

    def image(self, data: bytes, row: int, column: int) -> None:
        width, height = self.size()
        self.write(self.image_bytes(data, row, column) + f'{ESC}[?25l{ESC}[{height};{width}H'.encode())

    def key(self, timeout: float | None = None) -> str | None:
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        data = os.read(self.fd, 1)
        if not data:
            return 'q'
        if data == b'\x1b':
            if not select.select([self.fd], [], [], 0.05)[0]:
                return 'esc'
            prefix = os.read(self.fd, 1)
            if prefix not in (b'[', b'O'):
                return None
            sequence = bytearray()
            for _ in range(16):
                if not select.select([self.fd], [], [], 0.05)[0]:
                    return None
                char = os.read(self.fd, 1)
                if not char:
                    return None
                sequence.extend(char)
                if 0x40 <= char[0] <= 0x7e:
                    break
            else:
                return None
            final = bytes(sequence[-1:])
            if final in (b'A', b'B', b'C', b'D'):
                return {b'A': 'up', b'B': 'down', b'C': 'right', b'D': 'left'}[final]
            if prefix == b'[' and final == b'~':
                number = bytes(sequence[:-1]).split(b';', 1)[0]
                return {b'5': 'pageup', b'6': 'pagedown', b'1': 'home', b'4': 'end',
                        b'7': 'home', b'8': 'end'}.get(number)
            if prefix == b'[' and final in (b'H', b'F'):
                return 'home' if final == b'H' else 'end'
            return None
        if data[0] >= 0xc2:
            count = 1 if data[0] < 0xe0 else 2 if data[0] < 0xf0 else 3
            for _ in range(count):
                if not select.select([self.fd], [], [], 0.2)[0]:
                    return None
                data += os.read(self.fd, 1)
        return {b'\r': 'enter', b'\n': 'enter', b'\x7f': 'back', b'\x08': 'back', b' ': 'space'}.get(data, data.decode(errors='ignore'))

    def screen(self, title: str, hint: str) -> tuple[int, int]:
        width, height = self.size()
        self.write(f'{ESC}[?25l{ESC}[2J{ESC}[H{ESC}[1m{safe_text(title)[:width]}{ESC}[0m{ESC}[{height};1H{ESC}[2K{safe_text(hint)[:width]}')
        return width, height


def file_size(count: int) -> str:
    size = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return ''


def duration(value: object) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{remainder:02}' if hours else f'{minutes}:{remainder:02}'


def metadata(path: Path) -> list[str]:
    kind = media_kind(path)
    names = {'image': 'Kuva', 'video': 'Video', 'audio': 'Ääni'}
    try:
        stat = path.stat()
        lines = [f'Tyyppi: {names.get(kind, "Tiedosto")}', f'Koko: {file_size(stat.st_size)}']
        modified = f'Muokattu: {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M}'
    except OSError:
        return ['Tietoja ei voi lukea']
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                                capture_output=True, check=False, timeout=5)
        info = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info = {}
    streams = info.get('streams', [])
    picture = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    sound = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    fmt = info.get('format', {})
    if picture:
        width, height = picture.get('width'), picture.get('height')
        if width and height:
            lines.append(f'Mitat: {width} × {height}')
        if picture.get('codec_name'):
            lines.append(f'Kuva: {picture["codec_name"]}')
        if kind == 'video':
            rate = picture.get('avg_frame_rate', '0/0')
            try:
                numerator, denominator = map(int, rate.split('/'))
                if denominator:
                    lines.append(f'FPS: {numerator / denominator:g}')
            except (ValueError, ZeroDivisionError):
                pass
    length = duration(fmt.get('duration'))
    if length and kind != 'image':
        lines.append(f'Kesto: {length}')
    if sound:
        lines.append(f'Ääni: {sound.get("codec_name", "kyllä")}')
        if sound.get('sample_rate'):
            lines.append(f'Näyte: {sound["sample_rate"]} Hz')
    elif kind == 'video':
        lines.append('Ääni: ei ääniraitaa')
    tags = {str(k).lower(): safe_text(v) for k, v in fmt.get('tags', {}).items()}
    for key, label in (('title', 'Nimi'), ('artist', 'Artisti'), ('album', 'Albumi'), ('date', 'Vuosi')):
        if tags.get(key):
            lines.append(f'{label}: {tags[key]}')
    lines.append(modified)
    return lines


class PreviewCache:
    def __init__(self) -> None:
        self.items: dict[tuple[object, ...], tuple[list[str], bytes | None]] = {}

    def get(self, path: Path, format_: str, width: int, height: int) -> tuple[list[str], bytes | None]:
        try:
            stat = path.stat()
            key = (path, stat.st_mtime_ns, stat.st_size, format_, width, height)
        except OSError:
            return ['Tietoja ei voi lukea'], None
        if key in self.items:
            return self.items[key]
        lines = metadata(path)
        picture: bytes | None = None
        kind = media_kind(path)
        if kind in ('image', 'video'):
            source: bytes | None = None
            if kind == 'video':
                try:
                    still = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:V:0', '-frames:v', '1',
                                            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                           capture_output=True, check=False, timeout=5)
                    source = still.stdout if still.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired):
                    pass
            args = ['chafa', f'--format={format_}', '--animate=off', '--probe=off', '--scale=max',
                    f'--size={width}x{height}', '-' if source is not None else str(path)]
            if kind == 'image' or source is not None:
                try:
                    result = subprocess.run(args, input=source, capture_output=True, check=False, timeout=5)
                    if result.returncode == 0:
                        picture = result.stdout
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if len(self.items) >= 32:
            self.items.clear()
        self.items[key] = (lines, picture)
        return lines, picture


def box(term: Terminal, left: int, top: int, width: int, height: int, title: str) -> None:
    term.write(f'{ESC}[{top};{left}H┌{title[:width - 2]:─<{width - 2}}┐')
    for row in range(top + 1, top + height - 1):
        term.write(f'{ESC}[{row};{left}H│{ESC}[{row};{left + width - 1}H│')
    term.write(f'{ESC}[{top + height - 1};{left}H└{"─" * (width - 2)}┘')


def directory_dialog(term: Terminal, directory: Path) -> Path | None:
    """Read an absolute path or a path relative to the current directory."""
    width, height = term.size()
    box_width = min(64, max(12, width - 4))
    left = max(1, (width - box_width) // 2)
    top = max(2, (height - 5) // 2)
    value = ''
    error = ''
    while True:
        box(term, left, top, box_width, 5, ' Siirry hakemistoon ')
        text = '> ' + value
        term.write(f'{ESC}[{top + 2};{left + 1}H{text[-(box_width - 2):].ljust(box_width - 2)}')
        term.write(f'{ESC}[{top + 3};{left + 1}H{safe_text(error)[:box_width - 2].ljust(box_width - 2)}')
        term.park()
        key = term.key()
        if key == 'esc':
            return None
        if key == 'enter':
            if not value.strip():
                error = 'Kirjoita hakemiston nimi'
                continue
            path = Path(os.path.expandvars(value)).expanduser()
            if not path.is_absolute():
                path = directory / path
            path = path.resolve()
            if path.is_dir():
                return path
            error = 'Hakemistoa ei löydy'
        elif key == 'back':
            value = value[:-1]
            error = ''
        elif key == 'space':
            value += ' '
            error = ''
        elif key and len(key) == 1 and key.isprintable():
            value += key
            error = ''


def draw_browser(term: Terminal, directory: Path, paths: list[Path], index: int,
                 format_: str, cache: PreviewCache, video_window: bool = False, message: str = '') -> None:
    output_name = 'pygame' if video_window else 'chafa'
    indicator_width = len('output: ') + len(output_name)
    terminal_width, _ = term.size()
    title_width = max(0, terminal_width - indicator_width - 2)
    hint = '↑↓ selaa  PgUp/Dn sivu  Enter avaa  * vaihda  / polku  q lopeta'
    width, height = term.screen(str(directory)[:title_width], message or hint)
    column = max(1, width - indicator_width + 1)
    mode_color = 94 if video_window else 97
    term.write(f'{ESC}[1;{column}H{ESC}[97moutput: {ESC}[{mode_color}m{output_name}{ESC}[0m')
    preview = width >= 72 and height >= 14
    left_width = width * 52 // 100 if preview else width
    visible = max(1, height - 3)
    items = [None, *paths]
    start = max(0, min(index - visible // 2, len(items) - visible))
    for row, item in enumerate(items[start:start + visible], start=2):
        actual = start + row - 2
        label = '../' if item is None else safe_text(item.name) + ('/' if item.is_dir() else '')
        marker = '> ' if actual == index else '  '
        color = {'video': 91, 'image': 92, 'audio': 93}.get(media_kind(item)) if item is not None and item.is_file() else None
        if color:
            style = f'{ESC}[1;{color}' + (';4m' if actual == index else 'm')
        else:
            style = f'{ESC}[7m' if actual == index else ''
        term.write(f'{ESC}[{row};1H{style}{(marker + label)[:left_width]}{ESC}[0m')
    term.write(f'{ESC}[{height - 1};1H{ESC}[2K{index + 1}/{len(items)}')
    if preview:
        selected = items[index]
        right = left_width + 2
        panel_width = width - right
        thumb_height = max(5, min(12, (height - 3) // 2))
        details_top = thumb_height + 3
        details_height = height - details_top - 1
        box(term, right, 2, panel_width, thumb_height, ' Esikatselu ')
        box(term, right, details_top, panel_width, details_height, ' Tiedot ')
        if selected is not None:
            if selected.is_dir():
                lines, picture = ['Hakemisto', safe_text(selected.name)], None
            else:
                lines, picture = cache.get(selected, format_, panel_width - 2, thumb_height - 2)
            if picture:
                term.image(picture, 3, right + 1)
            elif selected.is_file():
                term.write(f'{ESC}[{3};{right + 1}H{("Ei kuvaesikatselua" if media_kind(selected) == "audio" else "Esikatselu ei saatavilla")[:panel_width - 2]}')
            for row, line in enumerate(lines[:details_height - 2], start=details_top + 1):
                term.write(f'{ESC}[{row};{right + 1}H{safe_text(line)[:panel_width - 2]}')
    term.park()


KITTY_PACKET = re.compile(rb'\x1b_G(.*?)\x1b\\', re.DOTALL)


def compress_kitty_frame(frame: bytes) -> bytes:
    """Compress Chafa's raw RGBA Kitty transfer when its packet layout is known."""
    packets = list(KITTY_PACKET.finditer(frame))
    if not packets or not packets[0].group(1).startswith(b'a=T,f=32,'):
        return frame
    if any(not packet.group(1).startswith(b'm=') for packet in packets[1:]):
        return frame
    if any(frame[a.end():b.start()] for a, b in zip(packets, packets[1:])):
        return frame
    controls = packets[0].group(1)
    if b';' in controls or b'o=' in controls:
        return frame
    try:
        fields = dict(part.split(b'=', 1) for part in controls.split(b','))
        expected = int(fields[b's']) * int(fields[b'v']) * 4
        encoded = b''.join(packet.group(1).split(b';', 1)[1] for packet in packets[1:]
                           if b';' in packet.group(1))
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, ValueError, binascii.Error):
        return frame
    if len(raw) != expected:
        return frame
    compressed = base64.b64encode(zlib.compress(raw, level=1))
    if len(compressed) >= len(encoded):
        return frame
    chunks = [compressed[i:i + 4096] for i in range(0, len(compressed), 4096)]
    head = controls.replace(b'm=1', b'm=' + (b'1' if len(chunks) > 1 else b'0')) + b',o=z'
    output = bytearray(frame[:packets[0].start()])
    output.extend(b'\x1b_G' + head + b';' + chunks[0] + b'\x1b\\')
    for i, chunk in enumerate(chunks[1:], start=1):
        more = b'1' if i < len(chunks) - 1 else b'0'
        output.extend(b'\x1b_Gm=' + more + b';' + chunk + b'\x1b\\')
    output.extend(frame[packets[-1].end():])
    return bytes(output)


def chafa_frame(path: Path, data: bytes | None, format_: str, width: int, height: int) -> bytes:
    args = ['chafa', f'--format={format_}', '--animate=off', '--probe=off', f'--size={max(1, width)}x{max(1, height - 3)}', '-' if data is not None else str(path)]
    try:
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise RuntimeError(f'Chafaa ei voi käynnistää: {exc}') from exc
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'Chafa ei voinut avata mediaa')
    return compress_kitty_frame(result.stdout) if format_ == 'kitty' else result.stdout


def present_frame(term: Terminal, path: Path, frame: bytes, format_: str, hint: str,
                  width: int, height: int) -> None:
    """Keep the old frame visible until the complete replacement is ready."""
    synchronized = f'{ESC}[?2026h'.encode() if format_ == 'kitty' else b''
    end_sync = f'{ESC}[?2026l'.encode() if format_ == 'kitty' else b''
    title = safe_text(path.name)[:width]
    footer = safe_text(hint)[:width]
    output = b''.join((
        synchronized,
        f'{ESC}[?25l{ESC}[2J{ESC}[H{ESC}[1m{title}{ESC}[0m'.encode(),
        term.image_bytes(frame, 2, 1),
        f'{ESC}[{height};1H{ESC}[2K{footer}{ESC}[?25l{ESC}[{height};{width}H'.encode(),
        end_sync,
    ))
    term.write(output)


def render(term: Terminal, path: Path, data: bytes | None, format_: str, hint: str) -> None:
    width, height = term.size()
    frame = chafa_frame(path, data, format_, width, height)
    present_frame(term, path, frame, format_, hint, width, height)


def show_image(term: Terminal, path: Path, format_: str) -> None:
    render(term, path, None, format_, 'Esc/q/Enter: takaisin')
    while term.key() not in ('esc', 'q', 'enter', 'back'):
        pass


def window_size(width: int, height: int, reduced: bool) -> tuple[int, int]:
    ratio = 0.33 if reduced else 1.0
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def draw_window_picture(pygame: object, picture: object) -> None:
    screen = pygame.display.get_surface()
    if screen is None:
        return
    width, height = picture.get_size()
    screen_width, screen_height = screen.get_size()
    ratio = min(screen_width / width, screen_height / height)
    target = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    if target != (width, height):
        picture = pygame.transform.smoothscale(picture, target)
    screen.fill((0, 0, 0))
    screen.blit(picture, ((screen_width - target[0]) // 2, (screen_height - target[1]) // 2))
    pygame.display.flip()


def show_image_window(term: Terminal, path: Path) -> None:
    """Show a still image at its original pixel size in a Pygame window."""
    import pygame

    try:
        pygame.display.init()
        try:
            picture = pygame.image.load(str(path))
        except pygame.error:
            result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                                     '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                                    capture_output=True, check=False)
            if result.returncode or not result.stdout:
                raise RuntimeError(f'Kuvaa ei voi avata: {path.name}')
            picture = pygame.image.load(io.BytesIO(result.stdout), 'image.png')
        width, height = picture.get_size()
        pygame.display.set_mode((width, height), pygame.RESIZABLE)
        pygame.display.set_caption(path.name)
        picture = picture.convert_alpha()
        draw_window_picture(pygame, picture)
        reduced = False
        while True:
            stop = False
            toggle_size = False
            redraw = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    stop = True
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        stop = True
                    elif event.key == pygame.K_f:
                        toggle_size = True
                elif event.type == pygame.VIDEORESIZE:
                    redraw = True
            key = term.key(0)
            if key in ('q', 'esc', 'enter', 'back'):
                stop = True
            elif key == 'f':
                toggle_size = True
            if stop:
                break
            if toggle_size:
                reduced = not reduced
                pygame.display.set_mode(window_size(width, height, reduced), pygame.RESIZABLE)
                redraw = True
            if redraw:
                draw_window_picture(pygame, picture)
            time.sleep(0.02)
    except pygame.error as exc:
        raise RuntimeError(f'Pygamen kuvaikkuna ei käynnistynyt: {exc}') from exc
    finally:
        pygame.display.quit()


def show_audio(term: Terminal, path: Path) -> None:
    """Stream one audio file through SDL_mixer without loading it into memory."""
    import pygame

    def draw(paused: bool) -> None:
        _, height = term.screen(path.name, 'Välilyönti: tauko/jatka  Esc/q: takaisin')
        term.write(f'{ESC}[{max(2, height // 2)};1H{("Tauko" if paused else "Toistetaan ääntä")}: {path.name}')
        term.park()

    try:
        pygame.mixer.init()
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        paused = False
        draw(paused)
        while True:
            key = term.key(0.1)
            if key in ('q', 'esc', 'enter', 'back'):
                break
            if key == 'space':
                if paused:
                    pygame.mixer.music.unpause()
                else:
                    pygame.mixer.music.pause()
                paused = not paused
                draw(paused)
            if not paused and not pygame.mixer.music.get_busy():
                break
    except pygame.error as exc:
        raise RuntimeError(f'Äänen toisto epäonnistui: {exc}') from exc
    finally:
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            pygame.mixer.quit()


def jpeg_frames(buffer: bytearray) -> list[bytes]:
    """Extract complete JPEG frames from an FFmpeg MJPEG image pipe."""
    frames = []
    while True:
        start = buffer.find(b'\xff\xd8')
        if start < 0:
            # The marker itself may be split across two pipe reads.
            trailing_ff = buffer.endswith(b'\xff')
            buffer.clear()
            if trailing_ff:
                buffer.append(0xff)
            break
        if start:
            del buffer[:start]
        end = buffer.find(b'\xff\xd9', 2)
        if end < 0:
            break
        frames.append(bytes(buffer[:end + 2]))
        del buffer[:end + 2]
    return frames


class VideoAudio:
    """Decode video audio into small PCM buffers queued on one mixer channel."""

    RATE = 44100
    CHUNK = RATE * 4 // 4  # 250 ms of stereo 16-bit audio

    def __init__(self, path: Path) -> None:
        import pygame

        self.pygame = pygame
        args = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0?', '-vn', '-ac', '2', '-ar', str(self.RATE), '-f', 's16le', '-']
        try:
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RuntimeError(f'FFmpegin äänipurku ei käynnistynyt: {exc}') from exc
        self.buffer = bytearray()
        self.eof = False
        self.ready = False
        self.channel = None

    @property
    def stdout(self):
        assert self.process.stdout is not None
        return self.process.stdout

    def wants_read(self) -> bool:
        return not self.eof and len(self.buffer) < self.CHUNK * 2

    def read(self) -> None:
        data = os.read(self.stdout.fileno(), 65536)
        if data:
            self.buffer.extend(data)
        else:
            self.eof = True

    def can_start(self) -> bool:
        return len(self.buffer) >= self.CHUNK or self.eof

    def _sound(self):
        length = min(len(self.buffer), self.CHUNK)
        length -= length % 4
        if not length or (length < self.CHUNK and not self.eof):
            return None
        data = bytes(self.buffer[:length])
        del self.buffer[:length]
        return self.pygame.mixer.Sound(buffer=data)

    def start(self) -> None:
        self.ready = True
        if not self.buffer:
            return  # Video without an audio stream.
        try:
            self.pygame.mixer.init(frequency=self.RATE, size=-16, channels=2, buffer=1024, allowedchanges=0)
            self.channel = self.pygame.mixer.Channel(0)
            sound = self._sound()
            if sound is not None:
                self.channel.play(sound)
        except self.pygame.error as exc:
            raise RuntimeError(f'Videon ääntä ei voi toistaa: {exc}') from exc

    def pump(self) -> None:
        if self.channel is None or self.channel.get_queue() is not None:
            return
        sound = self._sound()
        if sound is not None:
            if self.channel.get_busy():
                self.channel.queue(sound)
            else:
                self.channel.play(sound)

    def finished(self) -> bool:
        return self.eof and not self.buffer and (self.channel is None or not self.channel.get_busy())

    def pause(self) -> None:
        if self.channel is not None:
            self.channel.pause()

    def unpause(self) -> None:
        if self.channel is not None:
            self.channel.unpause()

    def close(self) -> None:
        if self.channel is not None:
            self.channel.stop()
        if self.pygame.mixer.get_init():
            self.pygame.mixer.quit()
        if self.process.poll() is None:
            self.process.terminate()
        self.stdout.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def show_video(term: Terminal, path: Path, format_: str) -> None:
    fps = 10
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-an', '-vf', f'fps={fps}', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-']
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f'FFmpegiä ei voi käynnistää: {exc}') from exc
    assert process.stdout is not None
    try:
        audio = VideoAudio(path)
    except Exception:
        process.terminate()
        process.stdout.close()
        process.wait()
        raise
    buffer = bytearray()
    frames: list[bytes] = []
    video_eof = False
    paused = False
    started: float | None = None
    pause_started = 0.0
    paused_duration = 0.0
    frame_index = 0
    last_frame: bytes | None = None
    last_encoded: bytes | None = None
    last_size: tuple[int, int] | None = None
    try:
        while True:
            key = term.key(0 if not paused else 0.05)
            if key in ('q', 'esc', 'back', 'enter'):
                break
            if key == 'space':
                paused = not paused
                if paused:
                    pause_started = time.monotonic()
                    audio.pause()
                else:
                    paused_duration += time.monotonic() - pause_started
                    audio.unpause()
                if last_frame is not None:
                    hint = 'Tauko — välilyönti: jatka  Esc/q: takaisin' if paused else 'Välilyönti: tauko  Esc/q: takaisin'
                    size = term.size()
                    if last_encoded is None or size != last_size:
                        last_encoded = chafa_frame(path, last_frame, format_, *size)
                        last_size = size
                    present_frame(term, path, last_encoded, format_, hint, *size)
            if paused:
                continue

            if started is None and (len(frames) >= 3 or video_eof) and frames and audio.can_start():
                last_frame = frames.pop(0)
                last_size = term.size()
                last_encoded = chafa_frame(path, last_frame, format_, *last_size)
                audio.start()
                started = time.monotonic()
                present_frame(term, path, last_encoded, format_, 'Välilyönti: tauko  Esc/q: takaisin', *last_size)
                frame_index = 1
            elif started is not None:
                audio.pump()
                elapsed = time.monotonic() - started - paused_duration
                target = int(elapsed * fps)
                while len(frames) > 1 and frame_index < target:
                    frames.pop(0)
                    frame_index += 1
                if frames and frame_index <= target:
                    last_frame = frames.pop(0)
                    last_size = term.size()
                    last_encoded = chafa_frame(path, last_frame, format_, *last_size)
                    present_frame(term, path, last_encoded, format_, 'Välilyönti: tauko  Esc/q: takaisin', *last_size)
                    frame_index += 1

            if video_eof and started is None and not frames:
                raise RuntimeError('FFmpeg ei voinut purkaa videota')
            if video_eof and not frames and audio.finished():
                break

            readers = []
            if not video_eof and len(frames) < 6:
                readers.append(process.stdout)
            if audio.wants_read():
                readers.append(audio.stdout)
            if readers:
                ready = select.select(readers, [], [], 0.02)[0]
                if process.stdout in ready:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if chunk:
                        buffer.extend(chunk)
                        frames.extend(jpeg_frames(buffer))
                    else:
                        video_eof = True
                        process.wait()
                if audio.stdout in ready:
                    audio.read()
            else:
                time.sleep(0.02)
    finally:
        audio.close()
        if process.poll() is None:
            process.terminate()
        process.stdout.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def video_window_properties(path: Path) -> tuple[int, int, float]:
    """Get the source size and a practical frame rate for the GUI decoder."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'V:0',
             '-show_entries', 'stream=width,height,avg_frame_rate', '-of', 'json', str(path)],
            capture_output=True, check=False, timeout=5,
        )
        streams = json.loads(result.stdout).get('streams', []) if result.returncode == 0 else []
        stream = streams[0]
        width, height = int(stream['width']), int(stream['height'])
        if width <= 0 or height <= 0:
            raise ValueError('Virheelliset videon mitat')
        rate = stream.get('avg_frame_rate', '30/1')
        numerator, denominator = map(int, rate.split('/'))
        fps = numerator / denominator if denominator else 30.0
        return width, height, min(30.0, max(1.0, fps))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise RuntimeError(f'Videon tietoja ei voi lukea: {exc}') from exc


def show_video_window(term: Terminal, path: Path) -> None:
    """Decode native-size frames with FFmpeg and show them in a Pygame window."""
    import pygame

    width, height, fps = video_window_properties(path)
    video = None
    audio = None
    try:
        pygame.display.init()
        pygame.display.set_mode((width, height), pygame.RESIZABLE)
        pygame.display.set_caption(path.name)
        args = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:V:0', '-an',
                '-vf', f'fps={fps:.6g}', '-q:v', '2', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-']
        video = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert video.stdout is not None
        audio = VideoAudio(path)
        pending = bytearray()
        frames: list[bytes] = []
        video_eof = False
        started: float | None = None
        paused = False
        pause_started = 0.0
        paused_duration = 0.0
        frame_index = 0
        last_frame: bytes | None = None
        reduced = False

        def draw(jpeg: bytes) -> None:
            picture = pygame.image.load(io.BytesIO(jpeg), 'frame.jpg')
            draw_window_picture(pygame, picture)

        while True:
            stop = False
            toggle_pause = False
            toggle_size = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    stop = True
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        stop = True
                    elif event.key == pygame.K_SPACE:
                        toggle_pause = True
                    elif event.key == pygame.K_f:
                        toggle_size = True
                elif event.type == pygame.VIDEORESIZE and last_frame is not None:
                    draw(last_frame)
            key = term.key(0)
            if key in ('q', 'esc', 'enter', 'back'):
                stop = True
            elif key == 'space':
                toggle_pause = True
            elif key == 'f':
                toggle_size = True
            if stop:
                break
            if toggle_size:
                reduced = not reduced
                pygame.display.set_mode(window_size(width, height, reduced), pygame.RESIZABLE)
                if last_frame is not None:
                    draw(last_frame)
            if toggle_pause:
                paused = not paused
                if paused:
                    pause_started = time.monotonic()
                    audio.pause()
                else:
                    paused_duration += time.monotonic() - pause_started
                    audio.unpause()
                pygame.display.set_caption(path.name + (' — Tauko' if paused else ''))
            if paused:
                time.sleep(0.02)
                continue

            if started is None and (len(frames) >= 3 or video_eof) and frames and audio.can_start():
                last_frame = frames.pop(0)
                audio.start()
                started = time.monotonic()
                draw(last_frame)
                frame_index = 1
            elif started is not None:
                audio.pump()
                target_index = int((time.monotonic() - started - paused_duration) * fps)
                while len(frames) > 1 and frame_index < target_index:
                    frames.pop(0)
                    frame_index += 1
                if frames and frame_index <= target_index:
                    last_frame = frames.pop(0)
                    draw(last_frame)
                    frame_index += 1

            if video_eof and started is None and not frames:
                raise RuntimeError('FFmpeg ei voinut purkaa videota')
            if video_eof and not frames and audio.finished():
                break
            readers = []
            if not video_eof and len(frames) < 6:
                readers.append(video.stdout)
            if audio.wants_read():
                readers.append(audio.stdout)
            if readers:
                ready = select.select(readers, [], [], 0.01)[0]
                if video.stdout in ready:
                    chunk = os.read(video.stdout.fileno(), 65536)
                    if chunk:
                        pending.extend(chunk)
                        frames.extend(jpeg_frames(pending))
                    else:
                        video_eof = True
                        video.wait()
                if audio.stdout in ready:
                    audio.read()
            else:
                time.sleep(0.01)
    except pygame.error as exc:
        raise RuntimeError(f'Pygamen videoikkuna ei käynnistynyt: {exc}') from exc
    except OSError as exc:
        raise RuntimeError(f'FFmpeg ei voinut avata videota: {exc}') from exc
    finally:
        if audio is not None:
            audio.close()
        if video is not None:
            if video.poll() is None:
                video.terminate()
            assert video.stdout is not None
            video.stdout.close()
            try:
                video.wait(timeout=2)
            except subprocess.TimeoutExpired:
                video.kill()
                video.wait()
        pygame.display.quit()


def browse(term: Terminal, start: Path, format_: str, video_window: bool = False) -> None:
    directory = start if start.is_dir() else start.parent
    selected: Path | None = start if start.is_file() else None
    message = ''
    cache = PreviewCache()
    while True:
        try:
            paths = entries(directory)
        except RuntimeError as exc:
            directory = directory.parent
            paths = entries(directory)
            message = str(exc)
        items = [None, *paths]
        index = items.index(selected) if selected in items else 0
        selected = None
        while True:
            draw_browser(term, directory, paths, index, format_, cache, video_window, message)
            message = ''
            key = term.key()
            if key in ('q', 'esc'):
                return
            if key == '*':
                video_window = not video_window
                continue
            if key == '/':
                target = directory_dialog(term, directory)
                if target is not None:
                    directory = target
                    break
                continue
            if key in ('down', 'j'):
                index = (index + 1) % len(items)
            elif key in ('up', 'k'):
                index = (index - 1) % len(items)
            elif key == 'pagedown':
                index = min(len(items) - 1, index + max(1, term.size()[1] - 3))
            elif key == 'pageup':
                index = max(0, index - max(1, term.size()[1] - 3))
            elif key == 'home':
                index = 0
            elif key == 'end':
                index = len(items) - 1
            elif key in ('back', 'left', 'h'):
                selected, directory = directory, directory.parent
                break
            elif key in ('enter', 'right', 'l'):
                item = items[index]
                if item is None:
                    selected, directory = directory, directory.parent
                    break
                if item.is_dir():
                    directory = item
                    break
                try:
                    if media_kind(item) == 'image':
                        if video_window:
                            show_image_window(term, item)
                        else:
                            show_image(term, item, format_)
                    elif media_kind(item) == 'video':
                        if video_window:
                            show_video_window(term, item)
                        else:
                            show_video(term, item, format_)
                    else:
                        show_audio(term, item)
                except RuntimeError as exc:
                    message = str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Päätteen tiedostoselain kuville, videoille ja äänitiedostoille')
    parser.add_argument('path', nargs='?', type=Path, default=Path.cwd(), help='aloitushakemisto tai mediatiedosto')
    parser.add_argument('--format', choices=('auto', 'kitty', 'sixels', 'symbols'), default='auto', help='Chafan grafiikkamuoto')
    parser.add_argument('--media-window', '--video-window', dest='video_window', action='store_true',
                        help='avaa kuvat ja videot Pygamen ikkunaan')
    args = parser.parse_args(argv)
    args.path = args.path.expanduser().resolve()
    if not args.path.exists():
        parser.error(f'Polkua ei löydy: {args.path}')
    if args.path.is_file() and not media_kind(args.path):
        parser.error(f'Tiedostomuotoa ei tueta: {args.path}')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('Käyttö vaatii vuorovaikutteisen päätteen')
    if not shutil.which('chafa'):
        parser.error('Chafa puuttuu PATH-polusta')
    if not shutil.which('ffmpeg'):
        parser.error('FFmpeg puuttuu PATH-polusta')
    try:
        with Terminal() as term:
            if args.path.is_file():
                if media_kind(args.path) == 'image':
                    if args.video_window:
                        show_image_window(term, args.path)
                    else:
                        show_image(term, args.path, graphics_format(args.format))
                elif media_kind(args.path) == 'video':
                    if args.video_window:
                        show_video_window(term, args.path)
                    else:
                        show_video(term, args.path, graphics_format(args.format))
                else:
                    show_audio(term, args.path)
            browse(term, args.path, graphics_format(args.format), args.video_window)
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f'cmedia: {exc}', file=sys.stderr)
        return 1
    return 0
