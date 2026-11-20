// Browser smoke test for the Packing Tracker page (optional; needs Playwright).
//
// Start the server first, then run:
//   BASE_URL=http://127.0.0.1:5639 TRACKER_PASSWORD=... OUT_DIR=/tmp/pt node tests/e2e_smoke.js
//
// The server's database should be empty when the test starts.
// Exits non-zero on the first failed check.

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:5639';
const PASSWORD = process.env.TRACKER_PASSWORD;
const OUT_DIR = process.env.OUT_DIR || '.';

function check(cond, msg) {
  if (!cond) throw new Error('FAILED: ' + msg);
  console.log('ok -', msg);
}

(async () => {
  if (!PASSWORD) throw new Error('Set TRACKER_PASSWORD');
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1300, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  const alerts = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('dialog', d => { alerts.push(d.type() + ': ' + d.message()); d.accept(); });

  // Seed boxes as the old browser-only version would have saved them, before the page script runs.
  await page.addInitScript(() => {
    if (!localStorage.getItem('packingTracker.seeded')) {
      localStorage.setItem('packingTracker.seeded', '1');
      localStorage.setItem('packingTracker.v1', JSON.stringify({ version: 1, boxes: [
        { id: 'legacy-1', number: 1, location: 'Old apartment', contents: ['Plates', 'Mugs'], notes: '', fragile: true },
        { id: 'legacy-2', number: 2, location: 'Old apartment', contents: ['Books'], notes: '', fragile: false }
      ] }));
    }
  });

  await page.goto(BASE_URL);
  await page.waitForSelector('#loginView:not([hidden])', { timeout: 5000 });
  check(await page.isHidden('#appMain'), 'login form shown when signed out');

  await page.fill('#loginPassword', 'definitely-wrong');
  await page.click('#loginBtn');
  await page.waitForFunction(() => document.getElementById('loginError').textContent.length > 0);
  check((await page.textContent('#loginError')).includes('Wrong password'), 'wrong password rejected');

  await page.fill('#loginPassword', PASSWORD);
  await page.click('#loginBtn');
  await page.waitForSelector('#appMain:not([hidden])');
  await page.waitForFunction(() => document.querySelectorAll('#boxTable tr').length === 2);
  check(alerts.some(a => a.includes('Copy them to the server')), 'offered to copy old browser boxes');
  check(alerts.some(a => a.includes('Copied 2 box(es)')), 'old browser boxes copied to server');
  check(await page.inputValue('#boxNumber') === '3', 'next box number auto-filled');

  // Add a box with the new fields.
  await page.fill('#boxRoom', 'Kitchen');
  await page.fill('#boxLocation', 'Old apartment');
  await page.fill('#boxContents', ['Pots', 'Pans', '<img src=x onerror=alert(1)>'].join('\n'));
  await page.check('#boxOpenFirst');
  await page.check('#boxFragile');
  await page.click('#saveBtn');
  await page.waitForFunction(() => document.querySelectorAll('#boxTable tr').length === 3);
  const row3 = await page.textContent('#boxTable tr:nth-child(3)');
  check(row3.includes('Kitchen') && row3.includes('OPEN FIRST') && row3.includes('FRAGILE'), 'room and flags shown in list');
  check(row3.includes('<img src=x onerror=alert(1)>'), 'HTML in contents shown as text, not executed');

  // A long box for label-fit checks.
  await page.fill('#boxNumber', '1024');
  await page.fill('#boxRoom', 'Master bedroom walk-in closet');
  await page.fill('#boxLocation', 'Storage unit across town');
  await page.fill('#boxContents', Array.from({ length: 40 }, (_, i) => 'Book volume ' + (i + 1)).join('\n'));
  await page.fill('#boxNotes', 'Heavy - lift with two people');
  await page.check('#boxOpenFirst');
  await page.check('#boxFragile');
  await page.click('#saveBtn');
  await page.waitForFunction(() => document.querySelectorAll('#boxTable tr').length === 4);
  check(await page.inputValue('#boxNumber') === '1025', 'number continues after highest box');

  // Room filter including "No room set".
  await page.selectOption('#roomFilter', { label: 'No room set (2)' });
  check((await page.$$('#boxTable tr')).length === 2, 'filter shows boxes without a room');
  await page.selectOption('#roomFilter', '');

  // Duplicate number is rejected by the server.
  await page.fill('#boxNumber', '1');
  await page.fill('#boxLocation', 'X');
  await page.click('#saveBtn');
  await page.waitForFunction(() => true);
  await page.waitForTimeout(300);
  check(alerts.some(a => a.includes('Box 1 already exists')), 'duplicate box number rejected');

  // Edit conflict: change box 2 behind the page's back, then save the stale edit.
  await page.click('#boxTable tr:nth-child(2) button:has-text("Edit")');
  await page.evaluate(async () => {
    const list = await (await fetch('/api/boxes')).json();
    const b = list.boxes.find(x => x.number === 2);
    await fetch('/api/boxes/' + b.id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'packing-tracker' },
      body: JSON.stringify({ number: 2, location: 'Truck', contents: ['Books'], rev: b.rev })
    });
  });
  await page.fill('#boxRoom', 'Office');
  await page.click('#saveBtn');
  await page.waitForTimeout(300);
  check(alerts.some(a => a.includes('changed on another device')), 'stale edit rejected instead of overwriting');
  await page.click('#cancelEditBtn');

  // Bulk move.
  await page.check('#selectAll');
  await page.fill('#bulkLocation', 'New apartment');
  await page.click('#bulkMoveBtn');
  await page.waitForFunction(() =>
    [...document.querySelectorAll('#boxTable tr td:nth-child(4)')].every(td => td.textContent === 'New apartment'));
  check(true, 'bulk move updated every box');

  // Data is on the server: a fresh browser context (no localStorage) sees it after signing in.
  const page2 = await (await browser.newContext()).newPage();
  await page2.goto(BASE_URL);
  await page2.waitForSelector('#loginView:not([hidden])', { timeout: 5000 });
  await page2.fill('#loginPassword', PASSWORD);
  await page2.click('#loginBtn');
  await page2.waitForFunction(() => document.querySelectorAll('#boxTable tr').length === 4);
  check(true, 'second device sees the same 4 boxes');
  await page2.screenshot({ path: path.join(OUT_DIR, 'app.png') });

  // Box view: opening a label's link shows the box and allows a one-tap move.
  const ids = await page.evaluate(async () => (await (await fetch('/api/boxes')).json()).boxes
    .reduce((m, b) => { m[b.number] = b.id; return m; }, {}));
  await page.evaluate(id => { window.location.hash = 'box=' + id; }, ids[3]);
  await page.waitForSelector('#boxDialog[open]');
  check((await page.textContent('#bvTitle')) === 'Box 3', 'scan link opens the right box');
  check((await page.textContent('#bvRoom')).includes('Kitchen'), 'box view shows the room');
  await page.click('#bvMoveButtons button:has-text("Truck")');
  await page.waitForFunction(() => document.getElementById('bvMoved').textContent.includes('Truck'));
  check((await page.textContent('#bvLocation')) === 'Now at: Truck', 'one-tap move updates the box');
  await page.fill('#bvMoveOther', 'Garage');
  await page.click('#bvMoveOtherBtn');
  await page.waitForFunction(() => document.getElementById('bvLocation').textContent === 'Now at: Garage');
  check(true, 'move to a typed location works');
  await page.click('#bvClose');
  await page.waitForFunction(() => window.location.hash === '' && !document.getElementById('boxDialog').open,
                             null, { timeout: 2000 });
  check(true, 'closing the box view clears the link');

  // A signed-out phone opening a scan link sees the box right after signing in.
  const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
  await phone.goto(BASE_URL + '/#box=' + ids[1024]);
  await phone.waitForSelector('#loginView:not([hidden])', { timeout: 5000 });
  await phone.fill('#loginPassword', PASSWORD);
  await phone.click('#loginBtn');
  await phone.waitForSelector('#boxDialog[open]');
  check((await phone.textContent('#bvTitle')) === 'Box 1024', 'scan link survives signing in on a phone');
  await phone.screenshot({ path: path.join(OUT_DIR, 'phone-box-view.png') });

  // Unknown box link.
  await page.evaluate(() => { window.location.hash = 'box=no-such-box'; });
  await page.waitForTimeout(200);
  check(alerts.some(a => a.includes("isn't in the tracker anymore")), 'unknown box link explained');

  // Printing: size dialog, copies, page size and fit for each label size.
  await page.evaluate(() => { window.__prints = 0; window.print = () => { window.__prints++; }; });
  await page.check('#selectAll');
  await page.click('#printSelectedBtn');
  check(await page.inputValue('#labelCopies') === '2', 'copies default to 2');
  await page.click('#sizeDialog button[value="cancel"]');
  await page.waitForTimeout(150);
  check(await page.evaluate(() => window.__prints) === 0, 'cancel does not print');

  for (const [size, copies] of [['4x6', 2], ['3x2', 1], ['2x1', 3]]) {
    await page.check('#selectAll');
    await page.click('#printSelectedBtn');
    await page.check(`input[name="labelSize"][value="${size}"]`);
    await page.fill('#labelCopies', String(copies));
    await page.click('#sizeDialog button[value="print"]');
    await page.waitForTimeout(150);
    const info = await page.evaluate(() => ({
      css: document.getElementById('pageSizeStyle').textContent,
      labels: document.querySelectorAll('#printArea .label').length
    }));
    const [w, h] = size.split('x');
    check(info.css.includes(`size: ${w}in ${h}in`), `${size}: print page size set`);
    check(info.labels === 4 * copies, `${size}: ${copies} cop(ies) of 4 labels = ${info.labels}`);
    const qr = await page.$$eval('#printArea .label', ls => ls.map(l => {
      const q = l.querySelector('.lbl-qr');
      return q ? { url: q.getAttribute('data-url'), svg: !!q.querySelector('svg path') } : null;
    }));
    if (size === '2x1') {
      check(qr.every(q => q === null), `${size}: no QR code`);
    } else {
      check(qr.every(q => q && q.svg && /\/#box=[A-Za-z0-9-]+$/.test(q.url)), `${size}: every label has a QR code linking to its box`);
      check(alerts.some(a => a.includes("which phones can't open")), `${size}: warned that localhost QR codes won't work on phones`);
    }
    await page.emulateMedia({ media: 'print' });
    const overflow = await page.$$eval('#printArea .label', labels => labels.map(l => {
      const lr = l.getBoundingClientRect();
      return [...l.querySelectorAll('*')].some(c => {
        const r = c.getBoundingClientRect();
        return r.height > 0 && (r.bottom > lr.bottom + 0.5 || r.right > lr.right + 0.5);
      });
    }));
    check(overflow.every(o => !o), `${size}: nothing runs off any label`);
    await page.pdf({ path: path.join(OUT_DIR, `labels-${size}.pdf`), preferCSSPageSize: true, printBackground: true });
    await page.emulateMedia({ media: 'screen' });
  }

  // Sign out.
  await page.click('#logoutBtn');
  await page.waitForSelector('#loginView:not([hidden])');
  check(true, 'sign out returns to login');

  check(pageErrors.length === 0, 'no JavaScript errors (' + pageErrors.join('; ') + ')');
  check(!alerts.some(a => a.includes('alert: 1')), 'no injected script ran');
  await browser.close();
  console.log('All browser checks passed.');
})().catch(err => {
  console.error(err.message || err);
  process.exit(1);
});
