// Use the temporary playwright-core installation; never add a project dependency.
const fs = require('node:fs');
const path = require('node:path');
const [base, output, packagePath] = process.argv.slice(2);
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('Local test service required');
process.env.PLAYWRIGHT_BROWSERS_PATH = path.resolve(packagePath, '../../browsers');
const { chromium } = require(packagePath);

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const requests = [];
  page.on('response', response => {
    if (response.url().startsWith(base) && !response.url().includes('/assets/')) requests.push({
      url: response.url().replace(base, ''), status: response.status(),
    });
  });
  try {
    await page.goto(base, { waitUntil: 'networkidle' });
    await page.locator('textarea').first().fill('这是界面验收。请只回答 UI_ACCEPTANCE_OK。');
    const submitted = page.waitForResponse(response => response.url().endsWith('/send-message') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const response = await submitted;
    if (!response.ok()) throw new Error(`UI submission failed: ${response.status()}`);
    const { prompt_id: runId } = await response.json();
    const answer = page.locator('.row:not(.user) .md').filter({ hasText: 'UI_ACCEPTANCE_OK' });
    await answer.first().waitFor({ state: 'visible', timeout: 180000 });
    await page.getByRole('button', { name: '发送', exact: true }).waitFor({ state: 'visible', timeout: 180000 });
    const state = await page.request.get(`${base}/runs/${runId}`);
    if (!state.ok()) throw new Error(`Run proxy returned ${state.status()}`);
    if ((await state.json()).status !== 'complete') throw new Error('UI run did not complete');
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('.list .item button.title').filter({ hasText: 'UI_ACCEPTANCE_OK' }).first().click();
    await answer.first().waitFor({ state: 'visible', timeout: 30000 });
    if (await answer.count() !== 1) throw new Error('Assistant content duplicated after reload');
    if (errors.length) throw new Error(errors.join('\n'));
    await page.screenshot({ path: path.join(output, 'browser.png'), fullPage: true });
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({
      passed: true, run_id: runId, history_reload: true, errors,
    }, null, 2));
    console.log('PASS: browser submit, SSE display, /runs proxy and history reload');
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'browser-failure.png'), fullPage: true });
    fs.writeFileSync(path.join(output, 'browser-failure.html'), await page.content());
    fs.writeFileSync(path.join(output, 'browser-failure.json'), JSON.stringify({ requests, errors }, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
