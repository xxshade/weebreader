"""Capture real demo screens and exercise their flows in Chrome AND Firefox.

Creates original sample books/artwork and a disposable server/database.
No production library, external source, tracker or credential is used.
See docs/screenshots.md for reproduction and browser flags.
"""
import argparse

from browser_checks import add_browser_argument, selected_browsers
import asyncio
from io import BytesIO
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import textwrap
import traceback
import unittest
import zipfile

from PIL import Image, ImageDraw, ImageFont
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resync_browser import BrowserChecks
from ci_report import failure

ROOT = Path(__file__).resolve().parents[1]
PROBE = """() => {
    window.demoErrors=[];
    Element.prototype.requestFullscreen=()=>Promise.resolve();
    window.addEventListener('error',e=>demoErrors.push(String(e.error||e.message)));
    window.addEventListener('unhandledrejection',e=>demoErrors.push(String(e.reason)));
}"""
TITLES = ('Night Tram', 'Atlas of Tides', 'Lantern Harbor', 'The Quiet Orbit',
          'Wildflower Station', 'The Glass Observatory', 'Letters from the North',
          'The Mapmaker\'s Garden')
COLORS = ((24, 47, 64), (32, 79, 87), (117, 56, 49), (48, 44, 81),
          (55, 83, 64), (50, 73, 94), (104, 69, 50), (84, 74, 45))
TEXT = [
    'The observatory had been dark for seven years when Mara found a light in its highest window. '
    'From the harbor road it looked no larger than a candle, but it kept time with the tide: '
    'three slow flashes, a pause, and three more. She stopped beside the last fishing boat and counted again.',
    'Everyone in Bellweather knew the old building. Its green dome stood above the town like an overturned '
    'teacup, weathered by salt and patient rain. Children drew it on their school maps. Sailors used it '
    'to find the harbor. Nobody could remember the last person who had climbed the hill to open its door.',
    'Mara had a key. It lay at the bottom of the envelope her grandmother had left her, wrapped in a '
    'small square of blue paper. There was no address on the envelope and no explanation inside. '
    'Only a sentence in the familiar, slanting handwriting: When the light returns, take the long way home.',
    'She took the narrow lane past the bakery, where tomorrow\'s bread was already rising. '
    'The windows glowed warm against the evening. Beyond the houses, the path turned into a stairway '
    'of uneven stones, each one holding a shallow pool of reflected sky.',
    'At the first landing she looked back. The harbor had become a collection of little lights, '
    'and somewhere below a tram bell sounded twice. It was an ordinary evening, she told herself. '
    'An ordinary evening on which someone had finally remembered to change a lamp.',
    'The key turned without resistance. Inside, the air smelled of paper, cedar, and a distant storm. '
    'A map covered the entrance hall from floor to ceiling. It showed every street in Bellweather, '
    'including one Mara had never seen: a narrow blue line that continued straight out into the sea.',
    'A note had been pinned beside that impossible street. This time the writing was her own. '
    'She held it close to the doorway and read the date twice. Tomorrow. The paper was dated tomorrow.',
    'Above her, the old telescope began to move. Its brass wheels turned as softly as leaves. '
    'The dome opened onto the first stars, and for a moment Mara could see the entire harbor '
    'reflected in the glass: every window, every boat, and one more light approaching from the water.',
]


def font(size, *, serif=False):
    name = 'DejaVuSerif.ttf' if serif else 'DejaVuSans.ttf'
    return ImageFont.truetype(name, size)


def cover(title, number, color):
    image = Image.new('RGB', (600, 900), color)
    draw = ImageDraw.Draw(image)
    cream, accent = '#f5e9d1', '#d8aa70'
    draw.rectangle((32, 32, 568, 868), outline=accent, width=2)
    draw.text((58, 60), 'COLLECTED STORIES', font=font(18), fill=accent)
    draw.ellipse((330, 125, 510, 305), fill=accent)
    for i in range(6):
        x = 55 + i * 82
        roof = 340 - ((i * 53 + number * 19) % 150)
        draw.rectangle((x, roof, x + 64, 480), outline=cream, width=2)
        for y in range(roof + 20, 460, 30):
            draw.line((x + 15, y, x + 49, y), fill=accent, width=3)
    for y in range(496, 535, 12):
        draw.line((58, y, 542, y), fill=accent, width=1)
    y = 570
    for line in textwrap.wrap(title.upper(), width=16):
        draw.text((58, y), line, font=font(42, serif=True), fill=cream)
        y += 58
    draw.text((58, 782), 'ROWAN VALE', font=font(21), fill=accent)
    draw.text((58, 824), f'VOLUME {number:02}', font=font(17), fill=cream)
    return image


def comic_page():
    image = Image.new('RGB', (1100, 1540), '#f3ecde')
    draw = ImageDraw.Draw(image)
    ink, light = '#203b4b', '#d3a665'
    draw.text((65, 38), 'NIGHT TRAM', font=font(35, serif=True), fill=ink)
    draw.text((65, 88), 'Chapter two  /  The last stop', font=font(20), fill=ink)
    for top, bottom in ((155, 680), (745, 1370)):
        draw.rectangle((60, top, 1040, bottom), fill=ink)
        draw.ellipse((780, top + 45, 910, top + 175), fill=light)
        for i in range(11):
            x = 70 + i * 88
            height = 90 + (i * 71) % 230
            draw.rectangle((x, bottom - height, x + 66, bottom), fill='#3c5964')
            for y in range(bottom - height + 15, bottom - 20, 27):
                draw.rectangle((x + 12, y, x + 24, y + 10), fill=light)
        for i in range(21):
            x, y = 100 + (i * 147) % 850, top + 35 + (i * 41) % 170
            draw.ellipse((x, y, x + 3, y + 3), fill='#f3ecde')
    draw.rounded_rectangle((180, 1070, 885, 1290), radius=40, fill=light, outline='#f3ecde', width=5)
    for x in range(220, 820, 120):
        draw.rectangle((x, 1100, x + 82, 1195), fill='#3c5964')
    draw.ellipse((245, 1260, 300, 1315), fill=ink)
    draw.ellipse((755, 1260, 810, 1315), fill=ink)
    draw.text((82, 704), 'At midnight, the city belonged to the last tram.', font=font(23, serif=True), fill=ink)
    draw.text((82, 1410), 'Tonight, someone was waiting at a stop that was not on the map.', font=font(23, serif=True), fill=ink)
    return image


def png(image):
    stream = BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def write_epub(path, title, artwork):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr('mimetype', 'application/epub+zip')
        book.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        book.writestr('book.opf', f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>Rowan Vale</dc:creator><dc:identifier id="uid">demo-{title}</dc:identifier><dc:language>en</dc:language></metadata><manifest><item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>''')
        body = '<h1>The Glass Observatory</h1><h2>Chapter one · The returning light</h2>' + ''.join(f'<p>{paragraph}</p>' for paragraph in TEXT)
        book.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The returning light</title></head><body>' + body + '</body></html>')
        book.writestr('cover.png', png(artwork))


def seed(fixture):
    fixture.stop_server()
    from app.library.scanner import scan_library
    from app.database import close_pool
    media = fixture.fixture / 'media'
    shutil.rmtree(media)
    (media / 'comics').mkdir(parents=True)
    (media / 'books').mkdir()
    with sqlite3.connect(fixture.data / 'library.db') as db:
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM series')
        db.execute('DELETE FROM libraries')
        db.execute("UPDATE users SET username='Reader' WHERE id=1")
        db.executemany('INSERT INTO libraries(id,name,root_path,media_type) VALUES(?,?,?,?)', [
            (1, 'Manga & Comics', str(media / 'comics'), 'comic'),
            (2, 'Novels', str(media / 'books'), 'ebook')])
    for i, (title, color) in enumerate(zip(TITLES, COLORS)):
        folder = media / ('comics' if i < 5 else 'books') / title
        folder.mkdir()
        for number in range(1, 5 if i == 0 else 3):
            art = cover(title, number, color)
            if i < 5:
                with zipfile.ZipFile(folder / f'Volume {number:02}.cbz', 'w') as archive:
                    archive.writestr('ComicInfo.xml', f'<ComicInfo><Title>{title} - Volume {number}</Title><Number>{number}</Number><Writer>Rowan Vale</Writer></ComicInfo>')
                    archive.writestr('001.png', png(art))
                    archive.writestr('002.png', png(comic_page()))
                    archive.writestr('003.png', png(cover('To be continued', number, color)))
            else:
                write_epub(folder / f'Volume {number:02}.epub', f'{title} - Volume {number}', art)

    async def scan():
        for library, directory in ((1, 'comics'), (2, 'books')):
            await scan_library(library, str(media / directory))
        await close_pool()
    asyncio.run(scan())
    with sqlite3.connect(fixture.data / 'library.db') as db:
        db.execute("UPDATE series SET summary='The last tram leaves at midnight. Follow a quiet journey through a city full of unexpected stories.', publication_status='completed' WHERE name='Night Tram'")
        comics = db.execute("SELECT items.id,items.series_id FROM items JOIN series ON series.id=items.series_id WHERE series.name='Night Tram' ORDER BY items.number_in_series").fetchall()
        text = db.execute("SELECT items.id FROM items JOIN series ON series.id=items.series_id WHERE series.name='The Glass Observatory' ORDER BY items.number_in_series").fetchone()[0]
        db.execute("INSERT INTO series_status(user_id,series_id,status) VALUES(1,?,'reading')", (comics[0][1],))
        db.execute("INSERT INTO reading_progress(user_id,item_id,status,progress_percent) VALUES(1,?,'completed',100)", (comics[0][0],))
        db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,progress_percent) VALUES(1,?,'reading',1,33)", (comics[1][0],))
        db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent) VALUES(1,?,'reading',0,'elidx:0',15)", (text,))
    fixture.start_server()
    return comics[0][1], comics[1][0], text


def capture(fixture, args, browser, series_id, comic_id, text_id):
    if browser == 'chrome':
        options = ChromeOptions()
        options.binary_location = args.chrome_binary
        for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage'):
            options.add_argument(flag)
        driver = webdriver.Chrome(options=options, service=ChromeService(args.chromedriver))
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '(' + PROBE + ')()'})
    else:
        options = FirefoxOptions()
        options.binary_location = args.firefox_binary
        options.add_argument('-headless')
        options.enable_bidi = True
        driver = webdriver.Firefox(options=options, service=FirefoxService(args.geckodriver,
            env={**os.environ, 'MOZ_DISABLE_CONTENT_SANDBOX': '1', 'XDG_CACHE_HOME': str(fixture.fixture / 'cache')},
            log_output=str(fixture.fixture / 'geckodriver.log')))
        driver.script.add_preload_script(PROBE)
    try:
        driver.set_window_size(1440, 1000)
        wait = WebDriverWait(driver, 20)
        js = driver.execute_script

        def ready(expression):
            return wait.until(lambda _: js('return ' + expression))

        def save(name):
            driver.execute_async_script("const done=arguments[0];document.fonts.ready.then(()=>requestAnimationFrame(()=>requestAnimationFrame(()=>done())))")
            if js('return demoErrors'):
                raise AssertionError(js('return demoErrors'))
            target = args.output_dir / browser / (name + '.png')
            target.parent.mkdir(parents=True, exist_ok=True)
            driver.save_screenshot(str(target))

        session = requests.Session()
        session.post(fixture.base + '/login', data={'username': 'Reader', 'password': 'disposable-browser-test-password'}, timeout=10).raise_for_status()
        driver.get(fixture.base + '/login')
        for name, value in session.cookies.items():
            driver.add_cookie({'name': name, 'value': value})
        js("localStorage.setItem('comic-reader-prefs',JSON.stringify({mode:'single',rtl:false,fit:'height'}));localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'paginated',theme:'dark',font:'Literata',fontSize:120,lineHeight:1.7,width:760,bottomGap:0}))")
        driver.get(fixture.base + '/')
        ready("document.querySelectorAll('#series-grid .series-card').length===8")
        ready("[...document.querySelectorAll('img')].filter(i=>i.getClientRects().length).every(i=>i.complete && i.naturalWidth>0)")
        save('library')
        driver.get(fixture.base + f'/series/{series_id}')
        ready("document.querySelectorAll('#items-grid [data-item-id]').length===4")
        ready("[...document.querySelectorAll('img')].filter(i=>i.getClientRects().length).every(i=>i.complete && i.naturalWidth>0)")
        save('series')
        driver.get(fixture.base + f'/reader/{comic_id}')
        ready("document.getElementById('page-image')?.complete && document.getElementById('page-image').naturalWidth>0")
        original = js("return document.getElementById('page-image').src")
        ActionChains(driver).send_keys(Keys.ARROW_LEFT).perform()
        ready("document.getElementById('page-image').src!==" + repr(original))
        ActionChains(driver).send_keys(Keys.ARROW_RIGHT).perform()
        ready("document.getElementById('page-image').src===" + repr(original))
        ready("document.getElementById('page-image').complete && document.getElementById('page-image').naturalWidth===1100")
        ActionChains(driver).move_to_element(driver.find_element('id', 'page-image')).click().perform()
        save('comic-reader')
        driver.get(fixture.base + f'/reader/{text_id}')
        ready("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        text = js("return document.querySelector('#paginated-viewer iframe').contentDocument.body.textContent")
        if 'The Glass Observatory' not in text or 'Mara' not in text:
            raise AssertionError('Sample EPUB did not render its original story')
        ActionChains(driver).move_to_element(driver.find_element('css selector', '#paginated-viewer iframe')).click().perform()
        driver.find_element('id', 'btn-next').click()
        ready("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible' && !document.querySelector('#paginated-viewer iframe').contentDocument.body.textContent.includes('The Glass Observatory')")
        next_position = js("return parseInt(document.getElementById('page-indicator').textContent,10)")
        driver.find_element('id', 'btn-prev').click()
        ready("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible' && parseInt(document.getElementById('page-indicator').textContent,10)<" + str(next_position))
        driver.find_element('id', 'btn-settings').click()
        ready("document.getElementById('settings-panel').getAttribute('aria-hidden')!=='true' && !document.getElementById('settings-panel').classList.contains('hidden')")
        save('text-reader')
        print(f'{browser}: four real screens, working page turns and settings, no JavaScript errors', flush=True)
    except Exception:
        print(driver.execute_script("return {title:document.title,errors:window.demoErrors,warning:document.getElementById('load-warning')?.textContent,loading:document.getElementById('loading-indicator')?.textContent}"), flush=True)
        print(driver.execute_script("return {text:document.querySelector('#paginated-viewer iframe')?.contentDocument?.body.textContent,indicator:document.getElementById('page-indicator')?.textContent}"), flush=True)
        print((fixture.fixture / 'server.log').read_text()[-6000:], flush=True)
        raise
    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox-esr')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    fixture = type('ScreenshotFixture', (BrowserChecks, unittest.TestCase), {})
    try:
        fixture.setUpClass()
        series, comic, text = seed(fixture)
        for browser in selected_browsers(args):
            # A previous browser's genuine page turns must not change the
            # next capture's starting positions.
            with sqlite3.connect(fixture.data / 'library.db') as db:
                db.execute("UPDATE reading_progress SET current_page=1,progress_percent=33 WHERE item_id=?", (comic,))
                db.execute("UPDATE reading_progress SET current_page=0,current_location='elidx:0',progress_percent=15 WHERE item_id=?", (text,))
            capture(fixture, args, browser, series, comic, text)
    finally:
        fixture.doClassCleanups()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        failure(traceback.format_exc())
        raise
