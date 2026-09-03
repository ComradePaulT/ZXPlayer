#!/usr/bin/env python3
"""ZXPlayer: 800 x 480 touch cassette deck. Run --help for options."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
from tape_engine import (AudioPlayer, DEVICE, EXTENSIONS, atomic_json,
                         block_index, cache_paths, render)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def timestamp(frames, rate):
    seconds = max(0, int(frames / rate))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def library_label(tape):
    """Display-only title/tags; never change paths used for playback or artwork."""
    stem=Path(tape).stem.replace('_',' ')
    groups=re.findall(r'\(([^()]*)\)|\[([^\[\]]*)\]',stem)
    machines=[];sides=[];parts=[]
    removable=[]
    for pair in groups:
        value=next(v for v in pair if v).strip() if any(pair) else ''
        if re.fullmatch(r'(?:16|48|128)\s*K?(?:\s*[-/]\s*(?:16|48|128)\s*K?)*',value,re.I):
            machines.append('-'.join(re.findall(r'16|48|128',value))+'K')
        elif re.fullmatch(r'Side\s+[A-Z0-9]+',value,re.I):
            sides.append('Side '+value.split()[-1].upper())
        elif re.fullmatch(r'Part\s+\d+(?:\s+of\s+\d+)?',value,re.I):
            parts.append(value.capitalize())
        else:continue
        removable.append(value)
    # A standalone year normally marks the start of TOSEC publisher/edition tags.
    title=re.split(r'\((?:\d{4}|\?{4})\)|\[',stem,maxsplit=1)[0].strip()
    for value in removable:
        title=re.sub(r'\(\s*'+re.escape(value)+r'\s*\)','',title,flags=re.I)
    title=re.sub(r'\s+',' ',title).strip(' -') or stem
    tags=list(dict.fromkeys(machines+sides+parts))
    return title,tags


def publisher_name(tape):
    stem=Path(tape).stem
    match=re.search(r'\((?:(?:19|20)\d{2}|\?{4})\)',stem)
    if match:
        publisher=re.search(r'\(([^()]*)\)',stem[match.end():])
        if publisher and publisher.group(1).strip():return publisher.group(1).strip()
    return 'Unknown publisher'


def group_name(tape, mode):
    if mode=='publisher':return publisher_name(tape)
    title=library_label(tape)[0]
    sortable=re.sub(r'^(?:the|an|a)\s+','',title.strip(),flags=re.I)
    first=next((c.upper() for c in sortable if c.isalnum()),'#')
    return first if first.isalpha() else '0–9' if first.isdigit() else '#'


ART_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif'}
UUID_SUFFIX = re.compile(r'[._ -][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
SCAN_SUFFIX = re.compile(r'-(\d{2,3})$')


def letters_and_numbers(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    return ''.join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def title_key(stem):
    # Collection tags start at a parenthesis/bracket; keep the full title before them.
    title = re.split(r'[\[(]', stem, maxsplit=1)[0].strip()
    article = re.search(r',\s*(the|an|a)$', title, re.I)
    if article:
        title = article.group(1) + ' ' + title[:article.start()]
    return letters_and_numbers(title)


def image_release(stem):
    duplicate = bool(UUID_SUFFIX.search(stem))
    stem = UUID_SUFFIX.sub('', stem)
    scan = SCAN_SUFFIX.search(stem)
    number = int(scan.group(1)) if scan else 0
    if scan:
        stem = stem[:scan.start()]
    return stem, (duplicate, bool(scan), number)


def publisher_keys(name):
    """Matching keys for publisher logo filenames, including common suffixes."""
    name=re.sub(r'[\s_-]+(?:logo|publisher)$','',str(name).strip(),flags=re.I)
    words=re.findall(r'[A-Za-z0-9]+',unicodedata.normalize('NFKD',name))
    keys=[letters_and_numbers(' '.join(words))]
    suffixes={'software','systems','consultants','games','entertainment','entertainments',
              'limited','ltd','inc','corporation','corp','group','computing'}
    shorter=list(words)
    while len(shorter)>1 and shorter[-1].casefold() in suffixes:
        shorter.pop()
        keys.append(letters_and_numbers(' '.join(shorter)))
    return [key for key in dict.fromkeys(keys) if key]


class ArtworkIndex:
    """Conservative title matching. Returned filenames are relative to images/."""
    def __init__(self, paths):
        self.paths = {p.casefold(): p for p in sorted(paths)}
        self.by_title = {'box': {}, 'tape': {}}
        self.by_publisher = {}
        self.cache = {}
        for path in sorted(paths):
            parts = path.split('/')
            if Path(path).suffix.lower() not in ART_EXTENSIONS:
                continue
            first = parts[0].casefold()
            if first in ('publisher','publishers'):
                for key in publisher_keys(Path(path).stem):
                    self.by_publisher.setdefault(key,[]).append(path)
                continue
            category = 'tape' if first in ('tape','tapes') else 'box' if first=='box' or len(parts)==1 else None
            if category is None:
                continue
            release, rank = image_release(Path(path).stem)
            entry = {'path': path, 'release': release, 'rank': rank,
                     'priority': 0 if first in ('box','tape') else 1}
            key = title_key(release)
            self.by_title[category].setdefault(key, []).append(entry)

    def resolve_publisher(self, name):
        for key in publisher_keys(name):
            matches=self.by_publisher.get(key,[])
            if matches:return sorted(matches,key=lambda p:(len(Path(p).stem),p.casefold()))[0]
        return None

    def resolve(self, tape, category='box'):
        key = (tape,category)
        if key in self.cache:
            return self.cache[key]
        stem = str(Path(tape).with_suffix('')).replace('\\','/')
        basename = Path(stem).name
        prefixes = ['box',''] if category=='box' else ['tape','tapes']
        for prefix in prefixes:
            for name in dict.fromkeys([stem,basename]):
                for ext in ('.png','.jpg','.jpeg','.webp','.bmp','.gif'):
                    candidate = f'{prefix}/{name}{ext}' if prefix else f'{name}{ext}'
                    path = self.paths.get(candidate.casefold())
                    if path:
                        options=sorted({path,*(e['path'] for e in self.by_title[category].get(title_key(basename),[]))})
                        result = (path,'exact',options)
                        self.cache[key]=result
                        return result
        entries = self.by_title[category].get(title_key(basename), [])
        if not entries and re.search(r' - Intro\s*(?:\(|$)', basename, re.I):
            entries = self.by_title[category].get(title_key(re.sub(r' - Intro(?=\s*(?:\(|$))','',basename,flags=re.I)), [])
        if entries:
            priority=min(e['priority'] for e in entries)
            entries=[e for e in entries if e['priority']==priority]
            groups={}
            for entry in entries:
                groups.setdefault(letters_and_numbers(entry['release']), []).append(entry)
            exact=groups.get(letters_and_numbers(basename))
            if exact:
                available=exact
                reason='release match'
            elif len(groups)==1:
                available=entries
                reason='title match'
            else:
                # Prefer a unique release whose explicit tags are also on the tape.
                tags=set(re.findall(r'[\[(]([^\])]+)[\])]',basename.casefold()))
                matching=[]
                for group in groups.values():
                    gt=set(re.findall(r'[\[(]([^\])]+)[\])]',group[0]['release'].casefold()))
                    if gt and gt.issubset(tags): matching.append(group)
                available=matching[0] if len(matching)==1 else []
                reason='release tags' if available else 'ambiguous'
            options=sorted(e['path'] for e in entries)
            if available:
                chosen=min(available,key=lambda e:(e['rank'],e['path'].casefold()))['path']
                result=(chosen,reason,options)
            else:
                result=(None,'ambiguous',options)
        else:
            result=(None,'no matching title',[])
        if result[0] is None and category=='tape' and not entries:
            for prefix in ('tape','tapes'):
                for ext in ('.png','.jpg','.jpeg','.webp','.bmp','.gif'):
                    path=self.paths.get(f'{prefix}/default{ext}')
                    if path:
                        result=(path,'default cassette',[path]); break
                if result[0]: break
        self.cache[key]=result
        return result


class DemoAudio:
    """Silent preview only; never selected during normal playback."""
    def __init__(self):
        self.state = dict(playing=False, frame=0, message='Preview - no audio output', error='')
    def send(self, command, value=None):
        if command == 'toggle':
            self.state['playing'] = not self.state['playing']
        elif command == 'seek':
            self.state['frame'] = value
    def close(self):
        pass


class App:
    def __init__(self, args):
        import pygame
        self.pg, self.args = pygame, args
        pygame.display.init()
        pygame.font.init()
        pygame.joystick.init()
        self.root = Path(args.root).expanduser().resolve()
        for folder in ('tapes', 'images', 'cache'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / 'settings.json'
        self.settings = read_json(self.settings_path, {})
        self.volume = max(0, min(100, int(self.settings.get('volume', 100))))
        self.model48 = self.settings.get('model48', True)
        self.device = self.settings.get('device', DEVICE)
        self.favourites = set(self.settings.get('favourites', []))
        self.audio = DemoAudio() if args.demo else AudioPlayer(self.device, self.volume, self.model48)
        flags = pygame.RESIZABLE if args.windowed else pygame.FULLSCREEN
        self.window = pygame.display.set_mode((800, 480) if args.windowed else (0, 0), flags)
        pygame.display.set_caption('ZXPlayer')
        pygame.mouse.set_visible(args.windowed)
        self.canvas = pygame.Surface((800, 480))
        font_path=Path(__file__).with_name('zxSpectrumStrict.ttf')
        self.zx_font=font_path.is_file()
        self.fallback_fonts={n:pygame.font.SysFont('DejaVu Sans',n) for n in (12,14,16,18,20,24,28,32)}
        self.fonts = {n: pygame.font.Font(str(font_path), n) if self.zx_font else self.fallback_fonts[n]
                      for n in (12, 14, 16, 18, 20, 24, 28, 32)}
        self.bg = (12, 13, 15)
        self.panel = (32, 34, 38)
        self.line = (66, 70, 76)
        self.ink = (245, 246, 240)
        self.muted = (163, 172, 181)
        self.gold = (0, 177, 222)
        self.green = (100, 215, 56)
        self.spectrum = [(245,48,48),(255,220,0),(81,192,30),(0,174,221)]
        self.brand_font = pygame.font.Font(str(font_path),28) if self.zx_font else pygame.font.SysFont('DejaVu Sans',24,bold=True)
        self.datacorder_font = pygame.font.SysFont('DejaVu Sans',25,bold=True,italic=True)
        self.fascia_font = pygame.font.SysFont('DejaVu Sans',15,bold=True,italic=True)
        def fascia_asset(name):
            try:
                return pygame.image.load(str(Path(__file__).with_name(name))).convert_alpha()
            except (OSError, pygame.error):
                return None
        self.sinclair_wordmark=fascia_asset('sinclair-wordmark.png')
        self.spectrum_wordmark=fascia_asset('zx-spectrum-wordmark.png')
        self.deck_view = self.settings.get('deck_view','cassette')
        if self.deck_view not in ('cassette','datacorder'):self.deck_view='cassette'
        self.group_mode=self.settings.get('group_mode','alphabetical')
        if self.group_mode not in ('alphabetical','publisher'):self.group_mode='alphabetical'
        self.current_group=None
        self.buttons, self.focus = [], 0
        self.screen = 'library'
        self.page = self.block_page = 0
        self.search = ''
        self.only_favourites = False
        self.selected = self.meta = self.pcm = None
        self.notice = ''
        self.job = self.job_log = None
        self.job_source = None
        self.art_job = self.art_job_log = None
        self.art_job_identity = self.art_job_category = None
        self.art_job_entry_id = None
        self.art_job_provider = 'zxdb'
        self.art_results_source = 'catalogue'
        self.art_job_kind = 'download'
        self.art_query = ''
        self.art_results = []
        self.art_result_page = self.art_result_total = 0
        self.art_search_warning = ''
        self.reel_angle = 0.0
        self.last_frame_time = time.monotonic()
        self.drag_volume = False
        self.art_cache = {}
        self.art_paths = {}
        self.hub_cache = {}
        self.alignment = None
        self.align_rect = None
        self.pick_category = 'box'
        self.pick_page = 0
        self.controllers = {}
        self.gamepad_focus = False
        self.scan()
        for i in range(pygame.joystick.get_count()):
            joystick = pygame.joystick.Joystick(i)
            self.controllers[joystick.get_instance_id()] = joystick
        if args.demo:
            self.tapes = [self.root / 'tapes' / f'{name}.tap' for name in
                          ['Midnight Circuit', 'Orbital Rescue', 'Pixel Kingdom', 'Solar Courier', 'The Last Outpost', 'Vector Valley']]
            self.selected = self.tapes[0]
            self.meta = {'rate': 44100, 'frames': 44100 * 245, 'blocks':
                         [{'frame': 0, 'label': '001  Program header'},
                          {'frame': 44100 * 8, 'label': '002  Loading screen'},
                          {'frame': 44100 * 50, 'label': '003  Game data'}], 'recording': False}
            self.audio.state.update(frame=44100 * 87, playing=True, message='Preview - no audio output')
            self.screen = args.preview_screen

    def scan(self):
        self.tapes = sorted((p for p in (self.root / 'tapes').rglob('*')
                             if p.is_file() and p.suffix.lower() in EXTENSIONS), key=lambda p: str(p).casefold())
        self.art_paths = {p.relative_to(self.root / 'images').as_posix().casefold(): p
                          for p in (self.root / 'images').rglob('*')
                          if p.is_file() and p.suffix.lower() in ART_EXTENSIONS}
        self.art_index = ArtworkIndex([p.relative_to(self.root / 'images').as_posix() for p in self.art_paths.values()])
        self.art_cache.clear()
        self.hub_cache.clear()

    def save(self):
        self.settings.update(volume=self.volume, device=self.device, model48=self.model48,
                             favourites=sorted(self.favourites),deck_view=self.deck_view,
                             group_mode=self.group_mode)
        try:
            atomic_json(self.settings_path, self.settings)
        except OSError as exc:
            self.notice = f'Could not save settings: {exc}'

    def identity(self, tape):
        return str(tape.relative_to(self.root / 'tapes'))

    def filtered(self):
        return [p for p in self.tapes if self.search.casefold() in p.stem.casefold()
                and (not self.only_favourites or self.identity(p) in self.favourites)
                and (self.search or self.only_favourites or self.current_group is None or
                     group_name(p,self.group_mode)==self.current_group)]

    def text(self, text, pos, size=18, colour=None, width=None, centre=False, pixel=False):
        text = str(text)
        # The authentic Spectrum face only has a small, unusually mapped glyph
        # set.  Keep it for fixed controls and headings; collection metadata,
        # filenames, numbers and punctuation use the complete readable face.
        spectrum_text=pixel and self.zx_font and all(32<=ord(c)<=126 for c in text)
        font = self.fonts[size] if spectrum_text else self.fallback_fonts[size]
        ellipsis='...' if spectrum_text else '…'
        if width:
            while text and font.size(text)[0] > width:
                trim=len(ellipsis)+1 if text.endswith(ellipsis) else 1
                text = text[:-trim] + ellipsis
        surface = font.render(text, not spectrum_text, colour or self.ink)
        rect = surface.get_rect(center=pos) if centre else surface.get_rect(topleft=pos)
        self.canvas.blit(surface, rect)
        return rect

    def rect(self, colour, area, radius=12, border=0):
        self.pg.draw.rect(self.canvas, colour, area, border, border_radius=radius)

    def label_lines(self, words, size, width, limit=2, pixel=False):
        font=self.fonts[size] if pixel and self.zx_font else self.fallback_fonts[size]
        lines=[];line=''
        for word in words:
            candidate=(line+' '+word).strip()
            if line and font.size(candidate)[0]>width:
                lines.append(line)
                if len(lines)==limit:
                    lines[-1]+='...' if pixel else '…'
                    return lines
                line=word
            else:line=candidate
        if line:lines.append(line)
        return lines

    def button(self, label, area, action, active=False, enabled=True, size=18):
        colour = self.gold if active else self.panel
        rect = self.pg.Rect(area)
        # Bevelled keycaps echo the Spectrum keyboard, with large touch targets.
        self.rect((3,4,5),rect,3)
        face=rect.inflate(-4,-5).move(0,-1)
        self.rect(colour if enabled else (23,25,28),face,2)
        edge=(109,218,245) if active and enabled else (79,83,88)
        self.pg.draw.line(self.canvas,edge,(face.left+2,face.top+1),(face.right-3,face.top+1))
        self.text(label, rect.center, size, (5,18,23) if active else self.ink if enabled else self.line,
                  centre=True,width=rect.width-14,pixel=True)
        if enabled:
            idx = len(self.buttons)
            self.buttons.append((rect, action))
            if self.gamepad_focus and idx == self.focus:
                self.rect(self.green, area, border=3)

    def transport_button(self, kind, area, action, active=False):
        self.button('',area,action,active=active)
        r=self.pg.Rect(area)
        colour=(5,18,23) if active else self.ink
        cy=r.y+21
        if kind in ('previous','next'):
            direction=-1 if kind=='previous' else 1
            for offset in (-8,8):
                cx=r.centerx+offset
                points=[(cx+direction*9,cy),(cx-direction*6,cy-9),(cx-direction*6,cy+9)]
                self.pg.draw.polygon(self.canvas,colour,points)
            line_x=r.centerx-28 if direction<0 else r.centerx+28
            self.pg.draw.line(self.canvas,colour,(line_x,cy-11),(line_x,cy+11),3)
            label='PREV BLOCK' if direction<0 else 'NEXT BLOCK'
        elif kind=='pause':
            self.pg.draw.rect(self.canvas,colour,(r.centerx-9,cy-11,6,22))
            self.pg.draw.rect(self.canvas,colour,(r.centerx+3,cy-11,6,22));label='PAUSE'
        elif kind=='play':
            self.pg.draw.polygon(self.canvas,colour,[(r.centerx-8,cy-11),(r.centerx-8,cy+11),(r.centerx+12,cy)]);label='PLAY'
        else:
            self.pg.draw.rect(self.canvas,colour,(r.centerx-9,cy-9,18,18));label='STOP'
        self.text(label,(r.centerx,r.bottom-12),12,colour,centre=True,width=r.width-12,pixel=True)

    def titlebar(self, title, subtitle):
        self.canvas.blit(self.brand_font.render('ZXPlayer',not self.zx_font,self.ink),(22,9))
        self.rainbow((22,44,139,10))
        self.text(title,(190,13),20,width=190 if self.screen=='art' else 318,pixel=True)
        self.text(subtitle,(191,42),12,self.muted,width=187 if self.screen=='art' else 308)
        self.pg.draw.line(self.canvas,(50,53,58),(22,64),(778,64))

    def rainbow(self, area):
        r=self.pg.Rect(area)
        clip=self.canvas.get_clip()
        self.canvas.set_clip(r.clip(clip))
        step=r.width/4
        for i,colour in enumerate(self.spectrum):
            x=r.left+i*step-r.height/2
            self.pg.draw.polygon(self.canvas,colour,[(x,r.top),(x+step,r.top),
                (x+step+r.height,r.bottom),(x+r.height,r.bottom)])
        self.canvas.set_clip(clip)

    def toggle_deck_view(self):
        self.deck_view='datacorder' if self.deck_view=='cassette' else 'cassette'
        self.save()

    def artwork(self, tape, category='box'):
        identity = Path(self.identity(tape)).as_posix()
        override = self.settings.get('artwork', {}).get(identity, {}).get(category)
        if override and override.casefold() in self.art_paths:
            return self.art_paths[override.casefold()]
        match, _, _ = self.art_index.resolve(identity, category)
        return self.art_paths.get(match.casefold()) if match else None

    def publisher_logo(self, name):
        match=self.art_index.resolve_publisher(name)
        return self.art_paths.get(match.casefold()) if match else None

    def load_art(self, path):
        if path is None:
            return None
        key = str(path)
        if key not in self.art_cache:
            image = None
            try:
                image = self.pg.image.load(str(path)).convert_alpha()
            except (self.pg.error, OSError):
                pass
            self.art_cache[key] = image
            if len(self.art_cache) > 80:
                self.art_cache.pop(next(iter(self.art_cache)))
        return self.art_cache.get(key)

    def fitted_art(self, image, area):
        rect = self.pg.Rect(area)
        scale = min(rect.width / image.get_width(), rect.height / image.get_height())
        size = (max(1, int(image.get_width()*scale)), max(1, int(image.get_height()*scale)))
        target = self.pg.Rect(0, 0, *size)
        target.center = rect.center
        self.canvas.blit(self.pg.transform.smoothscale(image, size), target)
        return target

    def cover(self, tape, area):
        rect = self.pg.Rect(area)
        key = self.identity(tape)
        image = self.load_art(self.artwork(tape))
        self.rect((9, 13, 18), rect, 6)
        if image:
            self.fitted_art(image, rect)
        else:
            digest = hashlib.sha256(key.encode()).digest()
            colour = tuple(45 + v % 70 for v in digest[:3])
            self.rect(colour, rect, 6)
            oldclip = self.canvas.get_clip()
            self.canvas.set_clip(rect)
            for n in range(6):
                x = rect.x + n * rect.width // 5
                self.pg.draw.line(self.canvas, tuple(min(255, v+35) for v in colour),
                                  (x, rect.bottom), (x+rect.width, rect.y), max(2, rect.width // 24))
            self.rect((20, 27, 34), (rect.x+8, rect.y+8, rect.width-16, 26), 2)
            self.text('ZX SPECTRUM', (rect.x+13, rect.y+13), 12, width=rect.width-25)
            # Placeholder designs are generated locally, not fetched game artwork.
            words = library_label(tape)[0].split()
            lines, line = [], ''
            for word in words:
                if self.fonts[18].size(line+' '+word)[0] > rect.width-20 and line:
                    lines.append(line)
                    line = word
                else:
                    line = (line+' '+word).strip()
            lines.append(line)
            for i, line in enumerate(lines[:4]):
                self.text(line, (rect.x+10, rect.centery-20+i*23), 18, width=rect.width-20)
            self.text('CASSETTE LIBRARY', (rect.x+10, rect.bottom-24), 12, width=rect.width-20)
            self.canvas.set_clip(oldclip)

    def library(self):
        if not self.search and not self.only_favourites and self.current_group is None:
            self.folder_library()
            return
        tapes = self.filtered()
        self.titlebar('Tape library', f'{len(tapes)} tapes  /  touch a cover to select')
        self.button('Search', (560, 12, 100, 44), lambda: self.go('search'))
        self.button('Player', (670, 12, 108, 44), lambda: self.go('deck'), enabled=bool(self.meta))
        self.button('Folders', (22, 72, 122, 42), self.open_folders)
        self.button('Favourites', (154, 72, 136, 42), lambda: self.filter_favs(True), self.only_favourites)
        place=self.search or (f'{"A-Z" if self.group_mode=="alphabetical" else "PUBLISHER"}: {self.current_group}' if self.current_group else 'ALL TAPES')
        self.text(place, (310, 84), 14, self.muted, width=335)
        self.button('Refresh', (674, 72, 104, 42), self.refresh, size=16)
        pages = max(1, math.ceil(len(tapes)/5))
        self.page = max(0, min(self.page, pages-1))
        for i, tape in enumerate(tapes[self.page*5:self.page*5+5]):
            x = 22 + i*154
            self.cover(tape, (x, 131, 140, 203))
            self.button('', (x, 340, 140, 74), lambda p=tape: self.select(p), size=14)
            title,tags=library_label(tape)
            for row,line in enumerate(self.label_lines(title.split(),14,124)):
                self.text(line,(x+8,345+row*18),14,width=124)
            for row,line in enumerate(self.label_lines(['['+t+']' for t in tags],12,124)):
                self.text(line,(x+8,383+row*14),12,self.gold,width=124)
            self.buttons.append((self.pg.Rect(x, 131, 140, 203), lambda p=tape: self.select(p)))
            if self.identity(tape) in self.favourites:
                self.text('★', (x+114, 135), 20, self.gold)
        if not tapes:
            self.text('No matching tapes', (400, 220), 28, centre=True)
            self.text('Add files to zxplayer/tapes, then tap Refresh.', (400, 260), 18, self.muted, centre=True)
        self.button('< Previous', (22, 421, 150, 44), lambda: self.turn_page(-1), enabled=self.page>0)
        self.text(f'{self.page+1} / {pages}', (240, 443), 18, self.muted, centre=True)
        self.button('Next >', (305, 421, 130, 44), lambda: self.turn_page(1), enabled=self.page<pages-1)
        self.button('Settings', (558, 421, 112, 44), lambda: self.go('settings'), size=16)
        self.button('Exit', (680, 421, 98, 44), lambda: self.go('exit'))

    def folder_library(self):
        counts={}
        for tape in self.tapes:
            name=group_name(tape,self.group_mode)
            counts[name]=counts.get(name,0)+1
        groups=sorted(counts,key=(lambda s:(s not in ('#','0–9'),s)) if self.group_mode=='alphabetical' else str.casefold)
        self.titlebar('Game folders',f'{len(self.tapes)} tapes  /  choose a folder')
        self.button('Search',(560,12,100,44),lambda:self.go('search'))
        self.button('Player',(670,12,108,44),lambda:self.go('deck'),enabled=bool(self.meta))
        self.button('A-Z',(22,72,126,42),lambda:self.set_group_mode('alphabetical'),self.group_mode=='alphabetical')
        self.button('Publisher',(158,72,154,42),lambda:self.set_group_mode('publisher'),self.group_mode=='publisher')
        self.text('VIRTUAL FOLDERS · FILES STAY IN PLACE',(329,84),12,self.muted,
                  width=210 if self.group_mode=='publisher' else 330)
        if self.group_mode=='publisher':
            self.button('Get logos',(548,72,116,42),self.fetch_publisher_logos,size=14)
        self.button('Refresh',(674,72,104,42),self.refresh,size=16)
        pages=max(1,math.ceil(len(groups)/10))
        self.page=max(0,min(self.page,pages-1))
        for i,name in enumerate(groups[self.page*10:self.page*10+10]):
            x=22+(i%5)*154;y=128+(i//5)*136
            self.button('',(x,y,140,112),lambda n=name:self.open_group(n))
            self.rect((45,48,51),(x+13,y+25,114,63),2)
            self.rect(self.gold,(x+13,y+16,54,13),1)
            logo=self.load_art(self.publisher_logo(name)) if self.group_mode=='publisher' else None
            if logo:
                # WOS thumbnails often sit on a large transparent 400x400
                # canvas. Crop that padding so the actual mark fills the tile.
                bounds=logo.get_bounding_rect(min_alpha=8)
                visible=logo.subsurface(bounds) if bounds.width and bounds.height else logo
                self.fitted_art(visible,(x+19,y+31,102,50))
            else:
                lines=self.label_lines(str(name).split(),16,114)
                for row,line in enumerate(lines):self.text(line,(x+13,y+40+row*19),16,width=114)
            self.text(f'{counts[name]} TAPES',(x+13,y+91),12,self.muted,width=114)
        if not groups:self.text('No tapes found',(400,235),24,centre=True)
        self.button('< Previous',(22,421,150,44),lambda:self.turn_page(-1),enabled=self.page>0)
        self.text(f'{self.page+1} / {pages}',(240,443),18,self.muted,centre=True)
        self.button('Next >',(305,421,130,44),lambda:self.turn_page(1),enabled=self.page<pages-1)
        self.button('Favourites',(558,421,112,44),lambda:self.filter_favs(True),size=14)
        self.button('Exit',(680,421,98,44),lambda:self.go('exit'))

    def set_group_mode(self,mode):
        self.group_mode=mode;self.current_group=None;self.page=0;self.save()

    def open_group(self,name):
        self.current_group=name;self.page=0;self.only_favourites=False

    def open_folders(self):
        self.current_group=None;self.only_favourites=False;self.search='';self.page=0

    def fetch_publisher_logos(self):
        if self.art_job:return
        self.art_job_kind='publisher-logos'
        self.art_job_log=tempfile.TemporaryFile(mode='w+b')
        self.art_job=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),
            '--fetch-publisher-logos','--root',str(self.root)],
            stdout=self.art_job_log,stderr=subprocess.STDOUT)

    def draw_photo_hubs(self, image, target, path, layout, angle):
        for cx, cy in layout['centres']:
            r = layout['radius']
            key = (str(path), cx, cy, r, target.width, target.height)
            radius = max(2, int(r * target.width))
            if key not in self.hub_cache:
                # Retain detail until after rotation so small teeth do not shimmer.
                factor = 3
                scaled = self.pg.transform.smoothscale(image, (target.width*factor,target.height*factor))
                crop_radius = radius*factor
                patch = self.pg.Surface((crop_radius*2+1, crop_radius*2+1), self.pg.SRCALPHA)
                # Flatten transparent spindle holes before rotation. Otherwise the
                # stationary teeth in the base photograph show through those holes.
                patch.fill((*self.bg, 255))
                patch.blit(scaled, (crop_radius-int(cx*target.width)*factor, crop_radius-int(cy*target.height)*factor))
                mask = self.pg.Surface(patch.get_size(), self.pg.SRCALPHA)
                self.pg.draw.circle(mask, (255,255,255,255), (crop_radius,crop_radius), crop_radius)
                patch.blit(mask, (0,0), special_flags=self.pg.BLEND_RGBA_MULT)
                self.hub_cache[key] = patch
                if len(self.hub_cache) > 40:
                    self.hub_cache.pop(next(iter(self.hub_cache)))
            centre = (target.x+int(cx*target.width), target.y+int(cy*target.height))
            # Clear the static hub as well as making the rotating crop opaque.
            # This also covers gaps from the rotated circular mask's pixel edges.
            self.pg.draw.circle(self.canvas, self.bg, centre, radius)
            rotated = self.pg.transform.rotozoom(self.hub_cache[key], -math.degrees(angle), 1/3)
            self.canvas.blit(rotated, rotated.get_rect(center=centre))

    def cassette(self, area, progress):
        path = self.artwork(self.selected, 'tape') if self.selected else None
        image = self.load_art(path)
        if image is not None:
            target = self.fitted_art(image, area)
            layout = self.settings.get('reel_layouts', {}).get(self.art_key(path))
            if layout:
                self.draw_photo_hubs(image,target,path,layout,self.reel_angle)
            else:
                self.rect(self.panel, (area[0]+6, area[1]+area[3]-29, 202, 26), 5)
                self.text('Tap cassette to align reels', (area[0]+13, area[1]+area[3]-24), 12, self.gold)
            return
        x,y,w,h = area
        self.rect((6, 9, 12), (x+3,y+5,w,h), 18)
        self.rect((187, 181, 160), area, 18)
        self.rect((219, 213, 189), (x+5,y+5,w-10,h-10), 15)
        for px,py in [(x+14,y+14),(x+w-14,y+14),(x+14,y+h-14),(x+w-14,y+h-14)]:
            self.pg.draw.circle(self.canvas, (111,113,103),(px,py),4)
            self.pg.draw.line(self.canvas, (61,67,62),(px-2,py),(px+2,py))
        self.rect((242, 233, 204), (x+26,y+17,w-52,53), 4)
        self.text(library_label(self.selected)[0] if self.selected else 'ZXPlayer', (x+38,y+23), 20, (45,51,49), width=w-110)
        self.text('COMPUTER CASSETTE  /  SIDE A', (x+38,y+49), 12, (93,97,83))
        for n,c in enumerate([(191,59,47),(219,159,43),(62,142,97),(43,127,160)]):
            self.rect(c, (x+26,y+73+n*5,w-52,5), 0)
        self.rect((34,43,43), (x+40,y+97,w-80,98), 13)
        centres = [(x+121,y+146),(x+w-121,y+146)]
        # Approximate tape pack area, with hub angular speed varying as radius changes.
        radii = [math.sqrt(18**2+(43**2-18**2)*(1-progress)), math.sqrt(18**2+(43**2-18**2)*progress)]
        for j,(cx,cy) in enumerate(centres):
            self.pg.draw.circle(self.canvas, (84,68,48),(cx,cy),int(radii[j]))
            for r in range(24,int(radii[j]),5):
                self.pg.draw.circle(self.canvas,(110,88,59),(cx,cy),r,1)
            self.pg.draw.circle(self.canvas, (216,211,185),(cx,cy),22)
            self.pg.draw.circle(self.canvas, (37,45,43),(cx,cy),11)
            angle = self.reel_angle * 35 / radii[j]
            for n in range(6):
                a = angle+n*math.tau/6
                p1=(cx+int(math.cos(a)*12),cy+int(math.sin(a)*12))
                p2=(cx+int(math.cos(a)*20),cy+int(math.sin(a)*20))
                self.pg.draw.line(self.canvas,(69,77,69),p1,p2,4)
        self.rect((172,185,162),(x+w//2-45,y+120,90,46),5)
        self.text('A', (x+w//2,y+143), 24, (54,67,58), centre=True)
        self.pg.draw.polygon(self.canvas,(157,155,137),[(x+95,y+h-9),(x+112,y+h-46),(x+w-112,y+h-46),(x+w-95,y+h-9)])
        for px in (x+130,x+w-130):
            self.pg.draw.circle(self.canvas,(45,53,48),(px,y+h-23),8)

    def datacorder(self, progress):
        # Code-drawn +2A-inspired case; the tape and its calibrated hubs are live.
        shell=self.pg.Rect(22,75,522,243)
        self.rect((3,4,5),shell.move(2,3),5)
        self.rect((39,41,44),shell,4)
        self.pg.draw.line(self.canvas,(91,94,97),(27,77),(539,77),2)
        self.pg.draw.line(self.canvas,(13,14,16),(25,316),(541,316),3)
        # Graphic wordmarks and a subtle offset shadow resemble the printed and
        # moulded fascia much better than UI-font labels.
        if self.sinclair_wordmark is not None:
            logo=self.pg.transform.smoothscale(self.sinclair_wordmark,(122,18))
            shadow=logo.copy();shadow.fill((60,28,25,150),special_flags=self.pg.BLEND_RGBA_MULT)
            self.canvas.blit(shadow,(40,85));self.canvas.blit(logo,(39,84))
        else:self.text('sinclair',(39,84),18,(229,75,58))
        mark=self.fascia_font.render('128K',True,(229,75,58))
        self.canvas.blit(mark,(324,85))
        if self.spectrum_wordmark is not None:
            logo=self.pg.transform.smoothscale(self.spectrum_wordmark,(112,22))
            shadow=logo.copy();shadow.fill((45,46,47,180),special_flags=self.pg.BLEND_RGBA_MULT)
            self.canvas.blit(shadow,(372,84));self.canvas.blit(logo,(371,83))
            plus=self.fascia_font.render('+2A',True,(244,244,238))
            self.canvas.blit(plus,(488,84))
        else:self.text('ZX Spectrum +2A',(371,87),14,self.ink)
        for x in range(31,538,12):self.rect((17,19,21),(x,106,7,3),0)
        glass=self.pg.Rect(43,116,480,147)
        self.rect((8,9,11),glass.inflate(8,8),2)
        clip=self.canvas.get_clip()
        self.canvas.set_clip(glass)
        self.rect((15,17,18),glass,0)
        photo=self.load_art(self.artwork(self.selected,'tape'))
        if photo is not None:
            # Fill the cassette window; the lower body sits behind its label plate.
            width=glass.width-32
            target=self.pg.Rect(glass.x+16,glass.y-32,width,max(1,int(width*photo.get_height()/photo.get_width())))
            self.canvas.blit(self.pg.transform.smoothscale(photo,target.size),target)
            path=self.artwork(self.selected,'tape')
            layout=self.settings.get('reel_layouts',{}).get(self.art_key(path))
            if layout:self.draw_photo_hubs(photo,target,path,layout,self.reel_angle)
        else:
            canvas=self.canvas
            tape=self.pg.Surface((522,243));tape.fill(self.bg)
            try:
                self.canvas=tape
                self.cassette((0,0,522,243),progress)
            finally:self.canvas=canvas
            self.canvas.blit(self.pg.transform.smoothscale(tape,(458,213)),(glass.x+11,glass.y-19))
        tint=self.pg.Surface(glass.size,self.pg.SRCALPHA)
        tint.fill((9,17,23,24))
        self.pg.draw.polygon(tint,(218,235,242,22),[(290,0),(330,0),(212,147),(172,147)])
        self.pg.draw.line(tint,(224,235,242,80),(3,2),(476,2),2)
        self.canvas.blit(tint,glass)
        self.canvas.set_clip(clip)
        self.rect((89,93,96),glass,0,border=1)
        plate=self.pg.Rect(43,267,480,33)
        self.rect((13,15,17),plate,1)
        word=self.datacorder_font.render('DATACORDER',True,(255,255,255))
        word=word.subsurface(word.get_bounding_rect()).copy()
        colours=self.pg.Surface(word.get_size(),self.pg.SRCALPHA)
        for i,colour in enumerate(self.spectrum):
            self.pg.draw.rect(colours,(*colour,255),(0,i*word.get_height()/4,word.get_width(),math.ceil(word.get_height()/4)))
        word.blit(colours,(0,0),special_flags=self.pg.BLEND_RGBA_MULT)
        word=self.pg.transform.smoothscale(word,(plate.width-40,25))
        for y in range(2,25,3):self.pg.draw.line(word,(0,0,0,0),(0,y),(word.get_width(),y))
        self.canvas.blit(word,word.get_rect(center=plate.center))
        self.pg.draw.circle(self.canvas,(8,10,11),(40,308),3)
        self.pg.draw.circle(self.canvas,self.green if self.audio.state['playing'] else (72,41,38),(523,308),3)
        self.text('Tap window to align cassette reels' if photo is not None else 'DATACORDER  /  EAR OUTPUT',
                  (57,302),12,self.muted,width=430)
        return glass

    def deck(self):
        if not self.meta:
            self.go('library')
            return
        state = self.audio.state.copy()
        frame = state['frame']
        progress = min(1,frame/max(1,self.meta['frames']))
        self.titlebar('+2A Datacorder' if self.deck_view=='datacorder' else 'Cassette deck', self.selected.name)
        self.button('Cassette view' if self.deck_view=='datacorder' else '+2A view',
                    (522,12,130,44),self.toggle_deck_view,size=14)
        self.button('Library', (664, 12, 114, 44), lambda: self.go('library'))
        if self.deck_view=='datacorder':cassette_target=self.datacorder(progress)
        else:
            self.cassette((22,75,522,243), progress)
            cassette_target=self.pg.Rect(22,75,522,243)
        if self.load_art(self.artwork(self.selected, 'tape')) is not None:
            self.buttons.append((cassette_target, self.start_alignment))
        self.cover(self.selected, (568,75,126,174))
        self.buttons.append((self.pg.Rect(568,75,126,174), lambda: self.go('art')))
        fav = self.identity(self.selected) in self.favourites
        self.button('Saved' if fav else 'Fav', (711,75,67,51), self.favourite, active=fav, size=14)
        self.button('Art', (711,136,67,51), lambda: self.go('art'))
        self.button('Set', (711,197,67,51), lambda: self.go('settings'))
        self.text('PLAYING' if state['playing'] else 'PAUSED / READY', (568,261), 14,
                  self.green if state['playing'] else self.gold)
        self.text(timestamp(frame,self.meta['rate'])+' / '+timestamp(self.meta['frames'],self.meta['rate']), (568,286), 16)
        self.rect(self.line,(24,329,752,5),2)
        self.rect(self.gold,(24,329,max(1,int(752*progress)),5),2)
        blocks = self.meta['blocks']
        idx = block_index(blocks,frame)
        label = blocks[idx]['label']
        self.text(f'{idx+1}/{len(blocks)}  {label}', (24,343), 14, self.muted, width=500)
        self.text(state['message'], (542,343), 12, self.green if state['playing'] else self.muted, width=235)
        self.transport_button('previous',(22,372,152,54),lambda:self.skip(-1))
        self.transport_button('pause' if state['playing'] else 'play',(185,372,188,54),lambda:self.audio.send('toggle'),active=True)
        self.transport_button('next',(384,372,152,54),lambda:self.skip(1))
        self.button('Bookmarks' if self.meta['recording'] else 'Blocks', (547,372,126,54), lambda: self.go('blocks'), size=16)
        self.transport_button('stop',(684,372,94,54),lambda:self.audio.send('seek',0))
        self.text('LEVEL', (24,447), 14, self.muted)
        self.button('−', (93,434,54,40), lambda: self.set_volume(self.volume-5), size=24)
        self.rect(self.line,(161,449,400,10),5)
        self.rect(self.gold,(161,449,max(1,int(400*self.volume/100)),10),5)
        self.pg.draw.circle(self.canvas,self.ink,(161+int(400*self.volume/100),454),10)
        self.buttons.append((self.pg.Rect(153,431,417,48), lambda: None))
        self.button('+', (577,434,54,40), lambda: self.set_volume(self.volume+5),size=24)
        self.text(f'{self.volume}%', (646,444),20,self.gold)
        self.text('EAR OUT', (714,448),12,self.muted)

    def blocks_screen(self):
        self.titlebar('Bookmarks' if self.meta['recording'] else 'Tape blocks', 'Select a position, then press Play')
        self.button('Back', (666,12,112,44), lambda: self.go('deck'))
        blocks = self.meta['blocks']
        pages = max(1,math.ceil(len(blocks)/5))
        self.block_page = min(max(0,self.block_page),pages-1)
        for i,b in enumerate(blocks[self.block_page*5:self.block_page*5+5]):
            label = timestamp(b['frame'],self.meta['rate'])+'   '+b['label']
            self.button(label,(22,77+i*63,756,54),lambda f=b['frame']: self.seek_and_deck(f),size=18)
        self.button('<', (22,413,70,50),lambda: self.block_turn(-1),enabled=self.block_page>0)
        self.text(f'{self.block_page+1}/{pages}',(144,438),18,centre=True)
        self.button('>', (194,413,70,50),lambda: self.block_turn(1),enabled=self.block_page<pages-1)
        if self.meta['recording']:
            self.button('Mark current position',(438,413,340,50),self.bookmark)

    def settings_screen(self):
        self.titlebar('Settings', 'Changes are saved on this Pi')
        self.button('Back',(666,12,112,44),lambda: self.go('deck' if self.meta else 'library'))
        self.text('Audio output',(24,90),20)
        self.button('Pi headphone jack',(280,77,244,54),lambda: self.set_device(DEVICE),self.device==DEVICE)
        self.button('System default',(536,77,242,54),lambda: self.set_device('default'),self.device=='default')
        self.text('Spectrum model',(24,163),20)
        self.button('48K',(280,150,244,54),lambda: self.set_model(True),self.model48)
        self.button('128K / +2 / +3',(536,150,242,54),lambda: self.set_model(False),not self.model48)
        self.text('Model setting controls tape-programmed 48K stop markers.',(24,222),16,self.muted)
        self.text('Controller',(24,270),20)
        self.text('Connected' if self.controllers else 'Connect a USB or paired Bluetooth gamepad',(280,273),16,self.muted,width=490)
        self.text('D-pad: move focus   A: select   B: back   Start: Play/Pause',(24,316),16)
        self.text('LB / RB: previous / next block   X / Y: volume down / up',(24,345),16)
        self.text('Keyboard: Space play/pause · Left/Right blocks · +/− volume',(24,387),14,self.muted)
        self.text('F11 fullscreen · Esc back · Q quit',(24,414),14,self.muted)
        self.text('Button numbers can be adjusted in settings.json (see START-HERE.txt).',(24,447),12,self.muted)

    def search_screen(self):
        artwork=self.screen=='art-search'
        value=self.art_query if artwork else self.search
        self.titlebar('Search '+self.pick_category+' artwork' if artwork else 'Find a tape',
                      'Type a title; search local images and the archive' if artwork else 'Search by title')
        self.button('Search' if artwork else 'Done',(666,12,112,44),self.submit_art_search if artwork else lambda: self.go('library'))
        if artwork:self.button('Search WOS',(516,12,138,44),lambda:self.submit_art_search('wos'),size=16)
        self.rect(self.panel,(22,76,756,55))
        self.text(value or 'Type a game title…',(38,90),24,self.ink if value else self.muted,width=720)
        for row,chars in enumerate(['QWERTYUIOP','ASDFGHJKL','ZXCVBNM123']):
            start = 24 + (10-len(chars))*37
            for i,char in enumerate(chars):
                self.button(char,(start+i*75,151+row*66,65,56),lambda c=char: self.add_search(c))
        self.button('Clear',(24,365,146,55),lambda: self.clear_search())
        self.button('Space',(183,365,283,55),lambda: self.add_search(' '))
        self.button('Delete',(479,365,145,55),self.delete_search)
        self.button('0 4–9',(637,365,139,55),lambda: self.go('art-numbers' if artwork else 'numbers'))
        self.text('Touch keys or use a physical keyboard; Enter searches.' if artwork else f'{len(self.filtered())} matching tapes',(24,441),16,self.muted)
        if artwork:self.button('Back',(666,431,112,44),lambda:self.go('pick-art'),size=16)

    def numbers_screen(self):
        artwork=self.screen=='art-numbers'
        self.titlebar('Search numbers',self.art_query if artwork else self.search)
        for i,char in enumerate('0123456789'):
            self.button(char,(25+(i%5)*154,115+(i//5)*90,138,74),lambda c=char:self.add_search(c),size=28)
        self.button('Letters',(24,360,230,60),lambda:self.go('art-search' if artwork else 'search'))
        self.button('Search' if artwork else 'Done',(547,360,230,60),self.submit_art_search if artwork else lambda:self.go('library'))

    def art_screen(self):
        self.titlebar('Box art',self.selected.stem)
        self.button('Choose box',(386,12,126,44),lambda:self.open_picker('box'),size=14)
        self.button('Choose tape',(524,12,130,44),lambda:self.open_picker('tape'),size=14)
        self.button('Back',(666,12,112,44),lambda:self.go('deck'))
        self.cover(self.selected,(24,74,752,390))

    def open_picker(self, category):
        self.pick_category,self.pick_page=category,0
        self.go('pick-art')

    def pick_art_screen(self):
        identity=Path(self.identity(self.selected)).as_posix()
        _,reason,options=self.art_index.resolve(identity,self.pick_category)
        self.titlebar('Choose '+self.pick_category+' image', 'Saved choice overrides automatic matching')
        self.button('Search title',(516,12,138,44),self.open_art_search,size=16)
        self.button('Back',(666,12,112,44),lambda:self.go('art'))
        pages=max(1,math.ceil(len(options)/4))
        self.pick_page=max(0,min(self.pick_page,pages-1))
        for i,path in enumerate(options[self.pick_page*4:self.pick_page*4+4]):
            x=22+i*194
            image=self.load_art(self.art_paths[path.casefold()])
            if image is not None:
                self.fitted_art(image,(x,84,176,215))
            self.text(Path(path).name,(x,311),14,self.muted,width=176)
            self.button('Use this',(x,341,176,48),lambda p=path:self.choose_art(p))
        if not options:
            self.text('No local images with a matching title',(400,190),24,centre=True)
            self.text('Add an image to images/'+self.pick_category+', then tap Refresh.',(400,238),16,self.muted,centre=True)
        self.button('<',(22,419,70,44),lambda:self.pick_turn(-1),enabled=self.pick_page>0)
        self.text(f'{self.pick_page+1}/{pages}',(142,440),16,centre=True)
        self.button('>',(192,419,70,44),lambda:self.pick_turn(1),enabled=self.pick_page<pages-1)
        self.button('Use automatic',(552,419,226,44),lambda:self.choose_art(None),size=16)
        self.button('Find online',(284,419,244,44),self.fetch_art,size=16)

    def open_art_search(self):
        self.art_query=re.split(r'[\[(]',self.selected.stem,maxsplit=1)[0].strip()
        self.go('art-search')

    def submit_art_search(self,provider='catalogue'):
        if self.art_job:return
        if not letters_and_numbers(self.art_query):
            self.notice='Enter a game title first.'
            return
        self.art_job_kind='wos-search' if provider=='wos' else 'search'
        self.art_job_log=tempfile.TemporaryFile(mode='w+b')
        self.art_job=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),
            '--wos-search' if provider=='wos' else '--search-art',self.pick_category,
            '--query',self.art_query,'--root',str(self.root)],
            stdout=self.art_job_log,stderr=subprocess.STDOUT)

    def art_results_screen(self):
        self.titlebar('WOS results' if self.art_results_source=='wos' else 'Artwork results',self.art_query)
        self.button('Edit search',(516,12,138,44),lambda:self.go('art-search'),size=16)
        self.button('Back',(666,12,112,44),lambda:self.go('pick-art'))
        pages=max(1,math.ceil(len(self.art_results)/4))
        self.art_result_page=max(0,min(self.art_result_page,pages-1))
        for i,result in enumerate(self.art_results[self.art_result_page*4:self.art_result_page*4+4]):
            y=78+i*79
            self.rect(self.panel,(22,y,756,69))
            self.text(result['title'],(35,y+7),20,width=562)
            self.text(result['detail'],(35,y+39),12,self.muted,width=562)
            self.button('Use' if result['kind']=='local' else 'Download',(611,y+9,156,51),lambda r=result:self.use_art_result(r),size=16)
        if not self.art_results:
            self.text('No matching images. Try a shorter or alternate title.',(400,210),20,centre=True)
        limit=len(self.art_results)
        self.text(f'{self.art_result_total} results'+(f' — first {limit} shown; narrow your search' if self.art_result_total>limit else ''),(24,401),14,self.muted)
        self.button('<',(22,428,70,44),lambda:self.art_result_turn(-1),enabled=self.art_result_page>0)
        self.text(f'{self.art_result_page+1}/{pages}',(142,450),16,centre=True)
        self.button('>',(192,428,70,44),lambda:self.art_result_turn(1),enabled=self.art_result_page<pages-1)
        is_wos=self.art_results_source=='wos'
        self.button('Search catalogue' if is_wos else 'Search WOS',(284,428,244,44),
                    lambda:self.submit_art_search('catalogue' if is_wos else 'wos'),size=16)
        self.text('Downloads need internet',(550,442),14,self.muted,width=225)

    def art_result_turn(self,direction):
        self.art_result_page+=direction

    def use_art_result(self,result):
        if result['kind']=='local':self.choose_art(result['path'])
        else:self.fetch_art(result['id'],'wos' if result['kind']=='wos' else 'zxdb')

    def fetch_art(self,entry_id=None,provider=None):
        if self.art_job: return
        self.art_job_kind='download'
        self.art_job_identity=Path(self.identity(self.selected)).as_posix()
        self.art_job_category=self.pick_category
        provider=provider or self.settings.get('artwork_provider',{}).get(self.art_job_identity,'zxdb')
        self.art_job_provider=provider
        if entry_id is None:entry_id=self.settings.get('wos_matches' if provider=='wos' else 'archive_matches',{}).get(self.art_job_identity)
        if provider=='wos' and entry_id is None:
            self.notice='Use Search WOS and select a title first.'
            return
        self.art_job_entry_id=entry_id
        self.art_job_log=tempfile.TemporaryFile(mode='w+b')
        command=[sys.executable,str(Path(__file__).resolve()),
            '--fetch-art',self.pick_category,'--tape-name',self.art_job_identity,
            '--root',str(self.root)]
        if entry_id is not None:command.extend(['--wos-id' if provider=='wos' else '--entry-id',str(entry_id)])
        self.art_job=subprocess.Popen(command,stdout=self.art_job_log,stderr=subprocess.STDOUT)

    def cancel_art_job(self):
        if self.art_job:
            self.art_job.terminate()
            try: self.art_job.wait(timeout=2)
            except subprocess.TimeoutExpired: self.art_job.kill(); self.art_job.wait()
            self.art_job=None
            self.art_job_log.close(); self.art_job_log=None

    def poll_art_job(self):
        if not self.art_job or self.art_job.poll() is None: return
        code=self.art_job.returncode
        self.art_job_log.seek(0)
        log=self.art_job_log.read().decode('utf-8','replace')
        self.art_job_log.close(); self.art_job_log=None; self.art_job=None
        if code:
            self.notice=log[-1000:] or 'Artwork download failed.'
            return
        try:
            result=json.loads(log)
            if self.art_job_kind=='publisher-logos':
                self.scan();self.page=0
                self.notice=result['message']
                return
            if self.art_job_kind in ('search','wos-search'):
                self.art_results_source='wos' if self.art_job_kind=='wos-search' else 'catalogue'
                self.art_results=result['results']
                self.art_result_total=result['total']
                self.art_result_page=0
                self.go('art-results')
                self.notice=result.get('warning','')
                return
            self.settings.setdefault('artwork',{}).setdefault(self.art_job_identity,{})[self.art_job_category]=result['path']
            if self.art_job_entry_id is not None:
                match_key='wos_matches' if self.art_job_provider=='wos' else 'archive_matches'
                self.settings.setdefault(match_key,{})[self.art_job_identity]=self.art_job_entry_id
                self.settings.setdefault('artwork_provider',{})[self.art_job_identity]=self.art_job_provider
            self.scan()
            self.save()
            self.go('deck')
            self.notice=result['message']
        except (OSError,ValueError,KeyError) as exc:
            self.notice='Could not use downloaded artwork: '+str(exc)

    def pick_turn(self, direction):
        self.pick_page+=direction

    def choose_art(self, path):
        identity=Path(self.identity(self.selected)).as_posix()
        choices=self.settings.setdefault('artwork',{}).setdefault(identity,{})
        if path is None: choices.pop(self.pick_category,None)
        else: choices[self.pick_category]=path
        self.save()
        self.go('deck')

    def art_key(self, path):
        return path.relative_to(self.root / 'images').as_posix()

    def start_alignment(self):
        path = self.artwork(self.selected, 'tape')
        if self.load_art(path) is None:
            return
        if self.audio.state['playing']:
            self.audio.send('toggle')
        saved = self.settings.get('reel_layouts', {}).get(self.art_key(path), {})
        self.alignment = {'path': path, 'centres': [], 'radius': saved.get('radius', 0.04)}
        self.go('align')

    def alignment_screen(self):
        count = len(self.alignment['centres'])
        instruction = ['Tap the centre of the LEFT spindle', 'Tap the centre of the RIGHT spindle',
                       'Moving preview: include every spindle tooth, then Save'][count]
        self.titlebar('Align cassette reels', instruction)
        self.button('Cancel', (666,12,112,44), lambda:self.go('deck'))
        image = self.load_art(self.alignment['path'])
        self.align_rect = self.fitted_art(image, (24,78,752,304))
        if count==2:
            self.draw_photo_hubs(image,self.align_rect,self.alignment['path'],self.alignment,time.monotonic()*2.5)
        self.buttons.append((self.align_rect, self.alignment_needs_touch))
        for i, (cx,cy) in enumerate(self.alignment['centres']):
            point = (self.align_rect.x+int(cx*self.align_rect.width), self.align_rect.y+int(cy*self.align_rect.height))
            radius = max(2,int(self.alignment['radius']*self.align_rect.width))
            self.pg.draw.circle(self.canvas,self.gold,point,radius,2)
            self.pg.draw.line(self.canvas,self.green,(point[0]-7,point[1]),(point[0]+7,point[1]),2)
            self.pg.draw.line(self.canvas,self.green,(point[0],point[1]-7),(point[0],point[1]+7),2)
            self.text(str(i+1),(point[0],point[1]-radius-13),16,self.gold,centre=True)
        self.text('Hub size', (24,397),16,self.muted)
        self.button('−', (124,391,60,50),lambda:self.adjust_hub(-0.005),size=24)
        self.button('+', (195,391,60,50),lambda:self.adjust_hub(0.005),size=24)
        self.button('Reset points',(280,391,177,50),self.reset_alignment,size=16)
        self.button('Save',(608,391,170,50),self.save_alignment,active=True,enabled=count==2)
        self.text('Circles must enclose all spindle teeth; keep the outer cassette body outside.',(24,454),12,self.muted)

    def alignment_needs_touch(self):
        self.notice = 'Tap or click the two spindle centres on the image to align the reels.'

    def adjust_hub(self, delta):
        self.alignment['radius'] = max(0.01,min(0.12,self.alignment['radius']+delta))

    def reset_alignment(self):
        self.alignment['centres'] = []

    def save_alignment(self):
        if len(self.alignment['centres']) == 2:
            self.settings.setdefault('reel_layouts', {})[self.art_key(self.alignment['path'])] = {
                'centres': self.alignment['centres'], 'radius': self.alignment['radius']}
            self.hub_cache.clear()
            self.save()
            self.go('deck')

    def draw(self):
        self.canvas.fill(self.bg)
        self.buttons = []
        if self.screen=='library': self.library()
        elif self.screen=='deck': self.deck()
        elif self.screen=='blocks': self.blocks_screen()
        elif self.screen=='settings': self.settings_screen()
        elif self.screen in ('search','art-search'): self.search_screen()
        elif self.screen in ('numbers','art-numbers'): self.numbers_screen()
        elif self.screen=='art-results': self.art_results_screen()
        elif self.screen=='art': self.art_screen()
        elif self.screen=='align': self.alignment_screen()
        elif self.screen=='pick-art': self.pick_art_screen()
        elif self.screen=='exit':
            self.titlebar('Close player?','Playback will stop')
            self.button('Keep playing',(140,205,245,65),lambda:self.go('deck' if self.meta else 'library'))
            self.button('Close player',(415,205,245,65),self.quit,active=True)
        if self.job:
            self.buttons=[]
            self.rect((8,13,18),(100,140,600,210),18)
            self.text('Preparing tape…',(400,185),28,self.gold,centre=True)
            self.text(self.job_source.name,(400,230),18,width=540,centre=True)
            self.text('Building audio and block index. First opening takes longer.',(400,271),14,self.muted,centre=True)
            self.button('Cancel',(320,294,160,44),self.cancel_job)
        if self.art_job:
            self.buttons=[]
            self.rect((8,13,18),(100,140,600,210),18)
            publisher_logos=self.art_job_kind=='publisher-logos'
            searching=self.art_job_kind in ('search','wos-search')
            wos=self.art_job_kind=='wos-search'
            self.text('Downloading publisher logos…' if publisher_logos else 'Searching titles…' if searching else 'Finding artwork…',(400,185),28,self.gold,centre=True)
            self.text('Matching your publisher folders with WOS…' if publisher_logos else 'Searching WOS Infoseek online…' if wos else 'Searching local images and the archive catalogue.' if searching else 'Original release first; re-release as a fallback.',(400,236),16,self.muted,centre=True)
            self.text('Existing logos are kept. This may take a few minutes.' if publisher_logos else 'No images are downloaded during search.' if searching else 'Archive mirrors can take a little time.',(400,269),14,self.muted,centre=True)
            self.button('Cancel',(320,294,160,44),self.cancel_art_job)
        error = self.notice or self.audio.state.get('error','')
        if error:
            self.buttons=[]
            self.rect((43,28,30),(48,114,704,272),16)
            self.text('ZXPlayer',(74,132),24,self.gold)
            words = error.split()
            lines, line = [], ''
            for word in words:
                if self.fonts[16].size(line+' '+word)[0]>647:
                    lines.append(line); line=word
                else: line=(line+' '+word).strip()
            lines.append(line)
            for i,line in enumerate(lines[:6]): self.text(line,(74,178+i*24),16)
            self.button('OK',(575,329,150,44),self.clear_error)
        if self.buttons: self.focus %= len(self.buttons)
        width,height = self.window.get_size()
        scale = min(width/800,height/480)
        size = (int(800*scale),int(480*scale))
        self.viewport = self.pg.Rect((width-size[0])//2,(height-size[1])//2,*size)
        self.window.fill((0,0,0))
        self.window.blit(self.pg.transform.smoothscale(self.canvas,size),self.viewport)
        self.pg.display.flip()

    def go(self, screen):
        self.screen,self.focus=screen,0
        if screen not in ('search','numbers'): self.save()

    def clear_error(self):
        self.notice=''
        self.audio.state['error']=''

    def quit(self): self.running=False
    def refresh(self): self.scan(); self.page=0
    def filter_favs(self,value): self.only_favourites=value; self.page=0
    def turn_page(self,n): self.page+=n; self.focus=0
    def block_turn(self,n): self.block_page+=n; self.focus=0
    def add_search(self,c):
        if self.screen in ('art-search','art-numbers'):self.art_query=(self.art_query+c)[:80]
        else:self.search=(self.search+c)[:80]; self.page=0
    def delete_search(self):
        if self.screen in ('art-search','art-numbers'):self.art_query=self.art_query[:-1]
        else:self.search=self.search[:-1]; self.page=0
    def clear_search(self):
        if self.screen in ('art-search','art-numbers'):self.art_query=''
        else:self.search=''; self.page=0
    def favourite(self):
        key=self.identity(self.selected)
        if key in self.favourites: self.favourites.remove(key)
        else: self.favourites.add(key)
        self.save()
    def set_volume(self,n):
        self.volume=max(0,min(100,int(n)))
        self.audio.send('volume',self.volume)
    def set_model(self,value):
        self.model48=value; self.audio.send('model48',value); self.save()
    def set_device(self,value):
        self.device=value; self.audio.send('device',value); self.save()
    def seek_and_deck(self,frame): self.audio.send('seek',frame); self.go('deck')
    def skip(self,direction):
        if not self.meta: return
        frame=self.audio.state['frame']
        blocks=self.meta['blocks']
        i=block_index(blocks,frame)
        if direction>0: i=min(len(blocks)-1,i+1)
        elif frame-blocks[i]['frame']<self.meta['rate']: i=max(0,i-1)
        self.audio.send('seek',blocks[i]['frame'])
        self.reel_angle+=direction*2
    def bookmark_path(self):
        key=hashlib.sha256(self.identity(self.selected).encode()).hexdigest()[:20]
        return self.root/'cache'/('bookmarks-'+key+'.json')
    def bookmark(self):
        frame=int(self.audio.state['frame'])
        blocks=self.meta['blocks']
        if all(abs(frame-b['frame'])>self.meta['rate']//2 for b in blocks):
            blocks.append({'frame':frame,'label':'Bookmark '+timestamp(frame,self.meta['rate'])})
            blocks.sort(key=lambda b:b['frame'])
            atomic_json(self.bookmark_path(),blocks)
    def select(self,tape):
        if self.args.demo:
            self.selected=tape; self.go('deck'); return
        if self.audio.state['playing']: self.audio.send('toggle')
        self.job_source=tape
        self.job_log=tempfile.TemporaryFile(mode='w+b')
        self.job=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--render',str(tape),
                                   '--root',str(self.root)],stdout=self.job_log,stderr=subprocess.STDOUT)
    def cancel_job(self):
        if self.job:
            self.job.terminate()
            try: self.job.wait(timeout=2)
            except subprocess.TimeoutExpired: self.job.kill(); self.job.wait()
            self.job=None
            self.job_log.close(); self.job_log=None
    def poll_job(self):
        if not self.job or self.job.poll() is None: return
        result=self.job.returncode
        self.job_log.seek(0)
        log=self.job_log.read().decode('utf-8','replace')
        self.job_log.close(); self.job_log=None; self.job=None
        if result:
            self.notice=log[-1000:] or 'Tape preparation failed.'
            return
        try:
            pcm,index=cache_paths(self.job_source,self.root/'cache')
            meta=json.loads(index.read_text())
            self.selected,self.meta,self.pcm=self.job_source,meta,pcm
            if meta['recording']:
                marks=read_json(self.bookmark_path(),meta['blocks'])
                self.meta['blocks']=sorted([b for b in marks if 0<=b['frame']<meta['frames']],key=lambda b:b['frame']) or meta['blocks']
            self.audio.send('load',(pcm,meta))
            self.go('deck')
        except Exception as exc: self.notice=str(exc)

    def pointer(self,pos,down=False,up=False):
        vx,vy,vw,vh=self.viewport
        point=((pos[0]-vx)*800/vw,(pos[1]-vy)*480/vh)
        if down:
            self.gamepad_focus=False
            if self.screen=='align' and self.align_rect and self.align_rect.collidepoint(point) and not self.notice:
                if len(self.alignment['centres']) < 2:
                    x=(point[0]-self.align_rect.x)/self.align_rect.width
                    y=(point[1]-self.align_rect.y)/self.align_rect.height
                    self.alignment['centres'].append([x,y])
                return
            if self.screen=='deck' and not self.notice and not self.job and not self.art_job and not self.audio.state.get('error') and self.pg.Rect(153,431,417,48).collidepoint(point):
                self.drag_volume=True
            else:
                for rect,action in self.buttons:
                    if rect.collidepoint(point): action(); break
        if self.drag_volume: self.set_volume((point[0]-161)*100/400)
        if up: self.drag_volume=False; self.save()

    def pad(self,button):
        mapping=self.settings.get('gamepad',{'select':0,'back':1,'volume_down':2,'volume_up':3,'previous':4,'next':5,'play':7})
        if button==mapping.get('select',0) and self.buttons: self.buttons[self.focus%len(self.buttons)][1]()
        elif button==mapping.get('back',1): self.go('deck' if self.meta and self.screen!='deck' else 'library')
        elif button==mapping.get('play',7) and self.meta: self.audio.send('toggle')
        elif button==mapping.get('previous',4): self.skip(-1)
        elif button==mapping.get('next',5): self.skip(1)
        elif button==mapping.get('volume_down',2): self.set_volume(self.volume-5); self.save()
        elif button==mapping.get('volume_up',3): self.set_volume(self.volume+5); self.save()

    def run(self):
        pg=self.pg
        self.running=True
        clock=pg.time.Clock()
        try:
            while self.running:
                self.poll_job()
                self.poll_art_job()
                now=time.monotonic()
                if self.audio.state['playing']: self.reel_angle+=(now-self.last_frame_time)*2.5
                self.last_frame_time=now
                self.draw()
                if self.args.screenshot:
                    pg.image.save(self.canvas,self.args.screenshot)
                    break
                for event in pg.event.get():
                    if self.art_job and event.type==pg.KEYDOWN:
                        if event.key==pg.K_ESCAPE:self.cancel_art_job()
                        continue
                    if self.art_job and event.type==pg.JOYBUTTONDOWN:
                        if event.button==self.settings.get('gamepad',{}).get('back',1):self.cancel_art_job()
                        continue
                    if event.type==pg.QUIT: self.go('exit')
                    elif event.type==pg.MOUSEBUTTONDOWN and event.button==1: self.pointer(event.pos,down=True)
                    elif event.type==pg.MOUSEBUTTONUP and event.button==1: self.pointer(event.pos,up=True)
                    elif event.type==pg.MOUSEMOTION and self.drag_volume: self.pointer(event.pos)
                    # SDL supplies mouse events for touch; do not process both and double-trigger.
                    elif event.type==pg.KEYDOWN:
                        if self.screen in ('search','numbers','art-search','art-numbers'):
                            if event.key==pg.K_BACKSPACE: self.delete_search()
                            elif event.key==pg.K_RETURN:
                                if self.screen in ('art-search','art-numbers'):self.submit_art_search()
                                else:self.go('library')
                            elif event.key==pg.K_ESCAPE:self.go('pick-art' if self.screen in ('art-search','art-numbers') else 'library')
                            elif event.unicode.isprintable(): self.add_search(event.unicode)
                        elif event.key==pg.K_ESCAPE: self.go('library' if self.screen=='deck' else 'deck' if self.meta else 'library')
                        elif event.key==pg.K_q: self.go('exit')
                        elif event.key==pg.K_F11: pg.display.toggle_fullscreen()
                        elif event.key==pg.K_SPACE and self.meta: self.audio.send('toggle')
                        elif event.key==pg.K_LEFT: self.skip(-1)
                        elif event.key==pg.K_RIGHT: self.skip(1)
                        elif event.key in (pg.K_PLUS,pg.K_EQUALS,pg.K_KP_PLUS): self.set_volume(self.volume+5)
                        elif event.key in (pg.K_MINUS,pg.K_KP_MINUS): self.set_volume(self.volume-5)
                        elif event.key==pg.K_TAB: self.gamepad_focus=True; self.focus+=1
                        elif event.key==pg.K_RETURN and self.buttons: self.buttons[self.focus%len(self.buttons)][1]()
                    elif event.type==pg.JOYDEVICEADDED:
                        joy=pg.joystick.Joystick(event.device_index); self.controllers[joy.get_instance_id()]=joy
                    elif event.type==pg.JOYDEVICEREMOVED: self.controllers.pop(event.instance_id,None)
                    elif event.type==pg.JOYHATMOTION:
                        self.gamepad_focus=True
                        x,y=event.value
                        if x or y: self.focus+=x-y
                    elif event.type==pg.JOYBUTTONDOWN: self.gamepad_focus=True; self.pad(event.button)
                clock.tick(30)
        finally:
            self.cancel_job()
            self.cancel_art_job()
            self.save()
            self.audio.close()
            pg.quit()


def main():
    parser=argparse.ArgumentParser(description='ZXPlayer touchscreen cassette deck')
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent),help='Folder containing tapes, images and cache')
    parser.add_argument('--windowed',action='store_true')
    parser.add_argument('--render',metavar='TAPE',help=argparse.SUPPRESS)
    parser.add_argument('--fetch-art',choices=['box','tape'],help=argparse.SUPPRESS)
    parser.add_argument('--search-art',choices=['box','tape'],help=argparse.SUPPRESS)
    parser.add_argument('--wos-search',choices=['box','tape'],help=argparse.SUPPRESS)
    parser.add_argument('--wos-id',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--fetch-publisher-logos',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--query',default='',help=argparse.SUPPRESS)
    parser.add_argument('--entry-id',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--tape-name',help=argparse.SUPPRESS)
    parser.add_argument('--demo',action='store_true',help='Silent visual preview; does not play a tape')
    parser.add_argument('--preview-screen',default='deck',choices=['deck','library','settings','blocks','art','search'])
    parser.add_argument('--screenshot',help='Save one frame as a PNG and exit')
    args=parser.parse_args()
    if args.fetch_publisher_logos:
        try:
            from publisher_wos import fetch_publisher_logos
            print(json.dumps(fetch_publisher_logos(Path(args.root))))
            return 0
        except Exception as exc:
            print(str(exc),file=sys.stderr)
            return 1
    if args.fetch_art or args.search_art or args.wos_search:
        try:
            from artwork_online import download_artwork, search_artwork
            catalog=Path(__file__).with_name('artwork_catalog.json')
            if args.wos_search:
                from artwork_wos import search_wos
                result=search_wos(args.query,args.wos_search,Path(args.root))
            elif args.wos_id is not None:
                from artwork_wos import download_wos
                result=download_wos(args.wos_id,args.fetch_art,Path(args.root))
            elif args.search_art:result=search_artwork(catalog,args.query,args.search_art,Path(args.root))
            else:result=download_artwork(catalog,title_key(Path(args.tape_name).stem),
                args.tape_name,args.fetch_art,Path(args.root),entry_id=args.entry_id)
            print(json.dumps(result))
            return 0
        except Exception as exc:
            print(str(exc),file=sys.stderr)
            return 1
    if args.render:
        try:
            render(Path(args.render),Path(args.root)/'cache')
        except Exception as exc:
            print(str(exc),file=sys.stderr)
            return 1
        return 0
    App(args).run()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
