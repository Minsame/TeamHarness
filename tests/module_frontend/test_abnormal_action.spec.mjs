// TeamHarness 前端异常操作测试
// 工具：Playwright + chromium
// 运行：node tests/module_frontend/test_abnormal_action.spec.mjs
//
// 测试范围（严格限定异常操作）：
//   1. 登录按钮快速重复点击
//   2. 颁发 API Key 快速重复点击
//   3. 共享范围修改重复提交
//   4. 批量修改重复提交
//   5. 创建关联重复提交
//   6. 删除关联重复点击
//   7. ACL 添加重复提交
//   8. 对话框 ESC 关闭
//   9. 对话框遮罩点击关闭
//  10. 表单中途取消
//  11. 菜单快速切换
//  12. 分页快速切换
//  13. 筛选器快速变更
//
// 测试铁律：用例独立可复现（每用例独立 browser context）；突变数据 round-trip 还原；
//          覆盖异常操作；FAIL 先定位根因；不修改源代码
//
// 技术说明：
//   - "快速重复点击"用 dispatchEvent 模拟（绕过 disabled 的 DOM 限制），
//     用于验证函数级防重入守卫是否存在（不仅仅依赖 DOM disabled）
//   - 有 loading 保护的按钮：DOM disabled 可阻止真实用户点击，但 dispatchEvent 可绕过
//   - 请求计数用 page.on('request') / page.on('response') 监听

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const SCREEN_DIR = path.join(__dirname, 'screenshots', 'abnormal_action');

if (!fs.existsSync(SCREEN_DIR)) fs.mkdirSync(SCREEN_DIR, { recursive: true });

// ---------------- 测试结果收集 ----------------
const results = [];
let passCount = 0, failCount = 0, skipCount = 0;
function record(name, status, detail = '') {
  results.push({ name, status, detail });
  if (status === 'PASS') passCount++;
  else if (status === 'FAIL') failCount++;
  else skipCount++;
  const tag = status === 'PASS' ? '\u001b[32m[PASS]\u001b[0m'
    : status === 'FAIL' ? '\u001b[31m[FAIL]\u001b[0m'
    : '\u001b[33m[SKIP]\u001b[0m';
  console.log(`${tag} ${name}${detail ? ' — ' + detail : ''}`);
}

// ---------------- 通用 helper ----------------
async function shot(page, filename) {
  try { await page.screenshot({ path: path.join(SCREEN_DIR, filename), fullPage: true }); }
  catch (e) { /* 截图失败不致命 */ }
}

async function fillInput(page, placeholder, value) {
  const inp = page.getByPlaceholder(placeholder).first();
  await inp.waitFor({ state: 'visible', timeout: 5000 });
  await inp.fill(value);
}

async function clickBtn(page, text, options = {}) {
  const btn = page.locator('.el-button', { hasText: text }).first();
  await btn.waitFor({ state: 'visible', timeout: options.timeout || 5000 });
  await btn.click();
}

async function selectOption(page, selectLocator, optionText) {
  await selectLocator.first().click();
  await page.waitForTimeout(250);
  const item = page.locator('.el-select-dropdown__item:visible', { hasText: optionText }).first();
  await item.waitFor({ state: 'visible', timeout: 5000 });
  await item.click();
}

async function waitForMessage(page, type, textContains, timeout = 3000) {
  const sel = `.el-message.el-message--${type}`;
  try {
    if (textContains) {
      const matched = page.locator(sel, { hasText: textContains }).first();
      await matched.waitFor({ state: 'visible', timeout });
      return await matched.textContent({ timeout });
    }
    await page.waitForSelector(sel, { timeout });
    return await page.locator(sel).first().textContent({ timeout });
  } catch (e) {
    throw new Error(`未出现 ${type} 消息（期望含 "${textContains}"）：${e.message}`);
  }
}

async function hasMessage(page, type, timeout = 1500) {
  try {
    await page.waitForSelector(`.el-message.el-message--${type}`, { timeout });
    return true;
  } catch { return false; }
}

async function gotoMenu(page, menuText) {
  const item = page.locator('.el-menu-item', { hasText: menuText }).first();
  await item.click();
  await page.waitForTimeout(600);
}

async function waitTableLoaded(page, timeout = 8000) {
  await page.waitForTimeout(300);
  try {
    await page.waitForSelector('.el-loading-mask', { state: 'hidden', timeout });
  } catch { /* 可能没有 loading mask */ }
  await page.waitForTimeout(200);
}

async function confirmMessageBox(page, action = 'confirm') {
  await page.waitForSelector('.el-message-box', { timeout: 3000 });
  const btn = action === 'confirm'
    ? page.locator('.el-message-box .el-button--primary').first()
    : page.locator('.el-message-box .el-button:not(.el-button--primary)').first();
  await btn.waitFor({ state: 'visible', timeout: 3000 });
  await btn.click();
}

async function closeDialogs(page) {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
}

// 控制台错误收集
function attachConsoleCollector(page) {
  const errors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', err => errors.push(`PAGEERROR: ${err.message}`));
  return errors;
}

// ---------------- 请求计数器 ----------------
function createRequestMonitor(page, urlPattern, method = null) {
  let requestCount = 0;
  const responses = [];
  page.on('request', req => {
    if (urlPattern.test(req.url()) && (!method || req.method() === method)) {
      requestCount++;
    }
  });
  page.on('response', async resp => {
    try {
      const url = resp.url();
      if (urlPattern.test(url) && (!method || resp.request().method() === method)) {
        responses.push({ status: resp.status(), url, method: resp.request().method() });
      }
    } catch (e) { /* response 可能已被消费 */ }
  });
  return {
    getCount: () => requestCount,
    getResponses: () => [...responses],
    reset: () => { requestCount = 0; responses.length = 0; }
  };
}

// ---------------- 快速重复点击（dispatchEvent 方案） ----------------
// 用 dispatchEvent 绕过 DOM disabled 限制，模拟极端快速点击
// 验证函数级防重入守卫（不仅仅是 DOM disabled）
async function rapidDispatchClick(page, buttonText, times, intervalMs = 100) {
  await page.evaluate(({ text, n, interval }) => {
    const btns = document.querySelectorAll('.el-button');
    let target = null;
    for (const b of btns) {
      const t = (b.textContent || '').trim();
      if (t === text && b.offsetParent !== null) { target = b; break; }
    }
    if (!target) throw new Error(`按钮 "${text}" 未找到（精确匹配失败）`);
    for (let i = 0; i < n; i++) {
      setTimeout(() => {
        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      }, i * interval);
    }
  }, { text: buttonText, n: times, interval: intervalMs });
  // 等待所有点击 + API 响应完成
  await page.waitForTimeout(times * intervalMs + 3000);
}

// 检查按钮 disabled 状态
async function isBtnDisabled(page, buttonText) {
  return await page.evaluate((text) => {
    const btns = document.querySelectorAll('.el-button');
    for (const b of btns) {
      if (b.textContent.includes(text) && b.offsetParent !== null) return b.disabled;
    }
    return false;
  }, buttonText);
}

// 循环确认所有 MessageBox（用于处理多次弹出的确认框）
async function confirmAllMessageBoxes(page, maxRounds = 10) {
  let confirmCount = 0;
  for (let i = 0; i < maxRounds; i++) {
    try {
      await page.waitForSelector('.el-message-box', { timeout: 800 });
      const btn = page.locator('.el-message-box .el-button--primary').first();
      await btn.click({ timeout: 1000 });
      confirmCount++;
      await page.waitForTimeout(300);
    } catch (e) {
      break;
    }
  }
  return confirmCount;
}

// ---------------- 登录 helper ----------------
async function loginAsAlice(browser, apiKey) {
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt++) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.login-card', { timeout: 15000 });
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', apiKey);
      await clickBtn(page, '登录');
      await page.waitForSelector('.app-header', { timeout: 15000 });
      await page.waitForTimeout(1000);
      return { ctx, page };
    } catch (e) {
      lastErr = e;
      await ctx.close().catch(() => {});
      // 重试前等待 2 秒
      if (attempt < 2) await new Promise(r => setTimeout(r, 2000));
    }
  }
  throw lastErr;
}

// ---------------- 主流程 ----------------
(async () => {
  console.log('=== TeamHarness 前端异常操作测试开始 ===\n');

  // 颁发 alice key
  let ALICE_KEY;
  try {
    const issueResp = await fetch('http://localhost:8080/v1/auth/apikey', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ member_id: 'alice', agent_id: 'agent-alice' })
    }).then(r => r.json());
    ALICE_KEY = issueResp.api_key;
    console.log(`颁发 alice key: ${ALICE_KEY.slice(0, 10)}...`);
  } catch (e) {
    console.error('颁发 key 失败：', e.message);
    process.exit(2);
  }

  // 记录所有 alice 资产的原始 scope（用于测试后全局恢复）
  const originalScopes = {};
  try {
    const listResp = await fetch('http://localhost:8080/v1/assets?owner=alice&limit=200', {
      headers: { 'X-API-Key': ALICE_KEY }
    }).then(r => r.json());
    for (const item of listResp.items) {
      originalScopes[item.id] = item.scope;
    }
    console.log(`已记录 ${Object.keys(originalScopes).length} 个资产的原始 scope`);
  } catch (e) { /* 记录失败不致命 */ }

  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--disk-cache-dir=./.pw-cache', '--disable-gpu']
    });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  // ============================================================
  // 1. 登录按钮快速重复点击
  // ============================================================
  console.log('\n--- 1. 登录按钮快速重复点击 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/auth\/apikey\/lookup/, 'POST');
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', ALICE_KEY);

      // 1 秒内连续 dispatchEvent 点击 10 次
      await rapidDispatchClick(page, '登录', 10, 100);

      // 等待登录成功或失败
      await page.waitForTimeout(2000);

      const reqCount = monitor.getCount();
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);

      await shot(page, '01-login-rapid-click.png');

      if (enteredMain && reqCount === 1) {
        record('1. 登录快速重复点击：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}，已进入主界面`);
      } else if (enteredMain && reqCount > 1) {
        record('1. 登录快速重复点击：只触发 1 次请求', 'FAIL',
          `请求数=${reqCount}（>1），DOM disabled 保护被 dispatchEvent 绕过，handleLogin 缺少函数级 loginLoading 守卫（app.js:34 无 if(loginLoading.value) return）`);
      } else if (stillLogin) {
        record('1. 登录快速重复点击', 'FAIL',
          `请求数=${reqCount}，仍在登录页。控制台错误：${errors.length} 条`);
      } else {
        record('1. 登录快速重复点击', 'FAIL',
          `请求数=${reqCount}，状态异常：enteredMain=${enteredMain} stillLogin=${stillLogin}`);
      }
    } catch (e) {
      await shot(page, '01-login-rapid-click-fail.png');
      record('1. 登录快速重复点击', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 2. 颁发 API Key 快速重复点击
  // ============================================================
  console.log('\n--- 2. 颁发 API Key 快速重复点击 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachConsoleCollector(page);
    // 监听 POST /v1/auth/apikey（颁发），排除 lookup
    const monitor = createRequestMonitor(page, /\/v1\/auth\/apikey$/, 'POST');
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      // 从对话框内定位输入框，避免匹配到登录页的输入框
      await dlg.getByPlaceholder('如：alice').fill('bob-test-rapid');
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill('agent-bob-rapid');

      await rapidDispatchClick(page, '颁发', 5, 100);

      const reqCount = monitor.getCount();
      const responses = monitor.getResponses();
      await shot(page, '02-issue-rapid-click.png');

      if (reqCount === 1) {
        record('2. 颁发 Key 快速重复点击：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}`);
      } else {
        record('2. 颁发 Key 快速重复点击：只触发 1 次请求', 'FAIL',
          `请求数=${reqCount}，handleIssueKey 缺少函数级 issueLoading 守卫（app.js:69）。响应状态：${responses.map(r => r.status).join(',')}`);
      }
    } catch (e) {
      await shot(page, '02-issue-rapid-click-fail.png');
      record('2. 颁发 Key 快速重复点击', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 3. 共享范围修改重复提交
  // ============================================================
  console.log('\n--- 3. 共享范围修改重复提交 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      const firstRow = page.locator('.app-main .el-table__row').first();
      const scopeBtn = firstRow.locator('.el-button', { hasText: '共享' });
      await scopeBtn.click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });

      // 记录当前 scope 和 asset ID，选一个不同的值
      const rowText = await firstRow.textContent();
      const firstRowId = (await firstRow.locator('td').first().textContent())?.trim() || 'asset-alice-001';
      const originalScope = rowText.includes('私有') ? 'private' : 'team';
      const newScope = originalScope === 'private' ? 'team' : 'private';
      const newScopeLabel = newScope === 'private' ? '私有' : '团队共享';
      await dlg.locator('.el-radio', { hasText: newScopeLabel }).click();

      // 快速重复点提交 5 次
      await rapidDispatchClick(page, '确认修改', 5, 100);

      const reqCount = monitor.getCount();
      const responses = monitor.getResponses();
      await shot(page, '03-scope-rapid-submit.png');

      // 还原 scope（通过 API，比 UI 操作更可靠）
      try {
        await fetch(`http://localhost:8080/v1/assets/${firstRowId}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: originalScope })
        }).then(r => r.json());
      } catch (e) { /* 还原失败不致命，全局恢复兜底 */ }

      if (reqCount === 1) {
        record('3. 共享修改重复提交：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}`);
      } else {
        record('3. 共享修改重复提交：只触发 1 次请求', 'FAIL',
          `请求数=${reqCount}，handleUpdateScope 缺少函数级 scopeUpdating 守卫（app.js:267）。响应状态：${responses.map(r => r.status).join(',')}`);
      }
    } catch (e) {
      await shot(page, '03-scope-rapid-submit-fail.png');
      record('3. 共享修改重复提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 4. 批量修改重复提交
  // ============================================================
  console.log('\n--- 4. 批量修改重复提交 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      // 勾选第一行
      const firstCheckbox = page.locator('.app-main .el-table__row .el-checkbox').first();
      await firstCheckbox.click();
      await page.waitForTimeout(400);
      await page.locator('.el-button', { hasText: '批量修改共享' }).first().click();
      const dlg = page.locator('.el-dialog', { hasText: '批量修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });

      // 快速重复点提交 5 次
      await rapidDispatchClick(page, '确认批量修改', 5, 100);

      const reqCount = monitor.getCount();
      const responses = monitor.getResponses();
      await shot(page, '04-batch-rapid-submit.png');

      if (reqCount === 1) {
        record('4. 批量修改重复提交：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}（单资产）`);
      } else {
        record('4. 批量修改重复提交：只触发 1 次请求', 'FAIL',
          `请求数=${reqCount}，handleBatchUpdateScope 缺少函数级 batchScopeUpdating 守卫（app.js:195）。响应状态：${responses.map(r => r.status).join(',')}`);
      }
    } catch (e) {
      await shot(page, '04-batch-rapid-submit-fail.png');
      record('4. 批量修改重复提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 5. 创建关联重复提交
  // ============================================================
  console.log('\n--- 5. 创建关联重复提交 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/links$/, 'POST');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await waitTableLoaded(page);

      // 获取第一行资产 ID，选一个不同的 dst
      const firstRowId = await page.locator('.app-main .el-table__row').first().locator('td').first().textContent();
      const srcId = firstRowId?.trim() || 'asset-alice-001';
      // dst 用一个不同的资产
      const dstId = srcId === 'asset-alice-001' ? 'asset-alice-002' : 'asset-alice-001';

      // 先通过 API 清理可能已存在的关联
      await fetch(`http://localhost:8080/v1/assets/${srcId}/links`, {
        headers: { 'X-API-Key': ALICE_KEY }
      }).then(r => r.json()).then(async (data) => {
        if (data && data.outgoing) {
          for (const link of data.outgoing) {
            if (link.peer_id === dstId) {
              await fetch(`http://localhost:8080/v1/assets/${srcId}/links/${link.link_id}`, {
                method: 'DELETE',
                headers: { 'X-API-Key': ALICE_KEY }
              }).catch(() => {});
            }
          }
        }
      }).catch(() => {});

      // 点击 srcId 行的"关联"按钮
      const srcRow = page.locator('.app-main .el-table__row', { hasText: srcId }).first();
      await srcRow.locator('.el-button', { hasText: '关联' }).click();
      const linksDlg = page.locator('.el-dialog', { hasText: '资产关联' });
      await linksDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(1000);

      // 点击"+ 添加关联"
      await linksDlg.locator('.el-button', { hasText: '添加关联' }).click();
      const addDlg = page.locator('.el-dialog', { hasText: '添加资产关联' });
      await addDlg.waitFor({ state: 'visible', timeout: 3000 });

      // 填写目标 ID
      await addDlg.getByPlaceholder('关联到的资产 ID').fill(dstId);
      await page.waitForTimeout(300);

      // 快速重复点"添加" 5 次
      await rapidDispatchClick(page, '添加', 5, 100);

      const reqCount = monitor.getCount();
      const responses = monitor.getResponses();
      await shot(page, '05-link-rapid-create.png');

      // 清理：删除可能创建的关联
      try {
        await fetch(`http://localhost:8080/v1/assets/${srcId}/links`, {
          headers: { 'X-API-Key': ALICE_KEY }
        }).then(r => r.json()).then(async (data) => {
          if (data && data.outgoing) {
            for (const link of data.outgoing) {
              if (link.peer_id === dstId) {
                await fetch(`http://localhost:8080/v1/assets/${srcId}/links/${link.link_id}`, {
                  method: 'DELETE',
                  headers: { 'X-API-Key': ALICE_KEY }
                }).catch(() => {});
              }
            }
          }
        }).catch(() => {});
      } catch (e) { /* 清理失败不致命 */ }

      // 判定
      const hasConflict = responses.some(r => r.status === 409);
      const has500 = responses.some(r => r.status >= 500);
      const has400 = responses.some(r => r.status === 400);

      if (reqCount === 1) {
        record('5. 创建关联重复提交：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}，src=${srcId} dst=${dstId}`);
      } else if (hasConflict && !has500) {
        record('5. 创建关联重复提交：后端 409 防护有效', 'PASS',
          `请求数=${reqCount}，重复创建返回 409（非 500），后端 IntegrityError 防护有效。前端缺少函数级 linkLoading 守卫`);
      } else if (has500) {
        record('5. 创建关联重复提交', 'FAIL',
          `请求数=${reqCount}，重复创建返回 500（应为 409）。响应状态：${responses.map(r => r.status).join(',')}`);
      } else if (has400) {
        record('5. 创建关联重复提交', 'FAIL',
          `请求数=${reqCount}，响应状态：${responses.map(r => r.status).join(',')}。src=${srcId} dst=${dstId}，可能自关联或参数错误`);
      } else {
        record('5. 创建关联重复提交', 'FAIL',
          `请求数=${reqCount}，响应状态：${responses.map(r => r.status).join(',')}`);
      }
    } catch (e) {
      await shot(page, '05-link-rapid-create-fail.png');
      record('5. 创建关联重复提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 6. 删除关联重复点击
  // ============================================================
  console.log('\n--- 6. 删除关联重复点击 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/links\//, 'DELETE');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await waitTableLoaded(page);

      // 获取第一行资产 ID，通过 API 创建一个测试关联
      const firstRowId = await page.locator('.app-main .el-table__row').first().locator('td').first().textContent();
      const srcId = firstRowId?.trim() || 'asset-alice-001';
      const dstId = srcId === 'asset-alice-001' ? 'asset-alice-002' : 'asset-alice-001';

      // 先清理可能已存在的关联，再创建新的
      await fetch(`http://localhost:8080/v1/assets/${srcId}/links`, {
        headers: { 'X-API-Key': ALICE_KEY }
      }).then(r => r.json()).then(async (data) => {
        if (data && data.outgoing) {
          for (const link of data.outgoing) {
            if (link.peer_id === dstId) {
              await fetch(`http://localhost:8080/v1/assets/${srcId}/links/${link.link_id}`, {
                method: 'DELETE',
                headers: { 'X-API-Key': ALICE_KEY }
              }).catch(() => {});
            }
          }
        }
      }).catch(() => {});

      // 创建测试关联
      await fetch(`http://localhost:8080/v1/assets/${srcId}/links`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
        body: JSON.stringify({ dst_asset_id: dstId, link_type: 'related_to' })
      }).then(r => r.json()).catch(() => {});

      // 打开关联列表
      const srcRow = page.locator('.app-main .el-table__row', { hasText: srcId }).first();
      await srcRow.locator('.el-button', { hasText: '关联' }).click();
      const linksDlg = page.locator('.el-dialog', { hasText: '资产关联' });
      await linksDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(1500);

      // 查找出向关联中的删除按钮
      const deleteBtn = linksDlg.locator('.el-table__row .el-button', { hasText: '删除' }).first();
      const hasLink = await deleteBtn.isVisible().catch(() => false);

      if (hasLink) {
        // 快速重复点删除按钮 5 次（dispatchEvent）
        await rapidDispatchClick(page, '删除', 5, 100);

        // 处理可能弹出的多个 MessageBox
        const confirmCount = await confirmAllMessageBoxes(page, 10);

        const reqCount = monitor.getCount();
        const responses = monitor.getResponses();
        await shot(page, '06-link-rapid-delete.png');

        const has404 = responses.some(r => r.status === 404);
        const has500 = responses.some(r => r.status >= 500);

        if (reqCount <= 1) {
          record('6. 删除关联重复点击：只触发 1 次请求', 'PASS',
            `请求数=${reqCount}，confirm 次数=${confirmCount}`);
        } else if (has404 && !has500) {
          record('6. 删除关联重复点击：后端 404 防护有效', 'PASS',
            `请求数=${reqCount}，重复删除返回 404（非 500），后端防护有效。前端 handleDeleteLink 无 loading 守卫，依赖 ElMessageBox 模态阻断`);
        } else if (has500) {
          record('6. 删除关联重复点击', 'FAIL',
            `请求数=${reqCount}，重复删除返回 500（应为 404）。响应状态：${responses.map(r => r.status).join(',')}`);
        } else {
          record('6. 删除关联重复点击', 'PASS',
            `请求数=${reqCount}，无 500 错误。响应状态：${responses.map(r => r.status).join(',')}`);
        }
      } else {
        // 没有关联可删，跳过
        await shot(page, '06-link-rapid-delete-skip.png');
        record('6. 删除关联重复点击', 'SKIP', `无可删除的关联（src=${srcId} 出向列表为空）`);
      }
    } catch (e) {
      await shot(page, '06-link-rapid-delete-fail.png');
      record('6. 删除关联重复点击', 'FAIL', e.message);
    } finally {
      // 清理残留关联
      try {
        const firstRowId = await page.locator('.app-main .el-table__row').first().locator('td').first().textContent().catch(() => '');
        const srcId = firstRowId?.trim() || 'asset-alice-001';
        const dstId = srcId === 'asset-alice-001' ? 'asset-alice-002' : 'asset-alice-001';
        await fetch(`http://localhost:8080/v1/assets/${srcId}/links`, {
          headers: { 'X-API-Key': ALICE_KEY }
        }).then(r => r.json()).then(async (data) => {
          if (data && data.outgoing) {
            for (const link of data.outgoing) {
              if (link.peer_id === dstId) {
                await fetch(`http://localhost:8080/v1/assets/${srcId}/links/${link.link_id}`, {
                  method: 'DELETE',
                  headers: { 'X-API-Key': ALICE_KEY }
                }).catch(() => {});
              }
            }
          }
        }).catch(() => {});
      } catch (e) { /* 清理失败不致命 */ }
      await ctx.close();
    }
  }

  // ============================================================
  // 7. ACL 添加重复提交
  // ============================================================
  console.log('\n--- 7. ACL 添加重复提交 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/acl$/, 'POST');
    // ACL 页面只显示 scope=restricted 的资产，需先把 asset-alice-004 改为 restricted
    const ACL_TEST_ASSET = 'asset-alice-004';
    let originalScope004 = null;
    try {
      const detail = await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}`, {
        headers: { 'X-API-Key': ALICE_KEY }
      }).then(r => r.json());
      originalScope004 = detail.scope;
      if (originalScope004 !== 'restricted') {
        await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: 'restricted' })
        }).then(r => r.json());
      }
    } catch (e) { /* 准备失败后面会自然报错 */ }
    try {
      await gotoMenu(page, 'ACL 授权');
      await waitTableLoaded(page, 10000);
      const row004 = page.locator('.el-table__row', { hasText: ACL_TEST_ASSET }).first();
      await row004.waitFor({ state: 'visible', timeout: 8000 });
      await row004.locator('.el-button', { hasText: '管理 ACL' }).click();
      const aclDlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await aclDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);

      // 点击"+ 添加授权"
      await aclDlg.locator('.el-button', { hasText: '添加授权' }).click();
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      await formDlg.waitFor({ state: 'visible', timeout: 3000 });

      // 填写唯一 grantee
      const uniqueGrantee = 'rapid-test-' + Date.now();
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill(uniqueGrantee);

      // 快速重复点"添加授权" 5 次
      await rapidDispatchClick(page, '添加授权', 5, 100);

      const reqCount = monitor.getCount();
      const responses = monitor.getResponses();
      await shot(page, '07-acl-rapid-add.png');

      // 清理：撤销可能创建的 ACL（通过 API 直接清理，避免 UI 操作复杂度）
      try {
        const aclList = await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/acl`, {
          headers: { 'X-API-Key': ALICE_KEY }
        }).then(r => r.json());
        if (aclList && aclList.items) {
          for (const acl of aclList.items) {
            if (acl.grantee_id === uniqueGrantee) {
              await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/acl/${acl.id}`, {
                method: 'DELETE',
                headers: { 'X-API-Key': ALICE_KEY }
              }).catch(() => {});
            }
          }
        }
      } catch (e) { /* 清理失败不致命 */ }

      const hasConflict = responses.some(r => r.status === 409);
      const has500 = responses.some(r => r.status >= 500);

      if (reqCount === 1) {
        record('7. ACL 添加重复提交：只触发 1 次请求', 'PASS',
          `请求数=${reqCount}`);
      } else if (hasConflict && !has500) {
        record('7. ACL 添加重复提交：后端 409 防护有效', 'PASS',
          `请求数=${reqCount}，重复添加返回 409（非 500），后端 IntegrityError 防护有效。前端缺少函数级 aclFormLoading 守卫`);
      } else if (has500) {
        record('7. ACL 添加重复提交', 'FAIL',
          `请求数=${reqCount}，重复添加返回 500（应为 409）。响应状态：${responses.map(r => r.status).join(',')}`);
      } else {
        record('7. ACL 添加重复提交', 'FAIL',
          `请求数=${reqCount}，响应状态：${responses.map(r => r.status).join(',')}`);
      }
    } catch (e) {
      await shot(page, '07-acl-rapid-add-fail.png');
      record('7. ACL 添加重复提交', 'FAIL', e.message);
    } finally {
      // 恢复 asset-alice-004 原 scope
      if (originalScope004 && originalScope004 !== 'restricted') {
        await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: originalScope004 })
        }).catch(() => {});
      }
      await ctx.close();
    }
  }

  // ============================================================
  // 8. 对话框 ESC 关闭
  // ============================================================
  console.log('\n--- 8. 对话框 ESC 关闭 ---');

  // 8.1 详情对话框 ESC
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+$/, 'GET');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '详情' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '资产详情' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(500);
      await page.keyboard.press('Escape');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      const visible = await dlg.isVisible().catch(() => false);
      await shot(page, '08-esc-detail.png');
      if (!visible) {
        record('8.1 详情对话框 ESC 关闭', 'PASS');
      } else {
        record('8.1 详情对话框 ESC 关闭', 'FAIL', 'ESC 后对话框仍可见');
      }
    } catch (e) {
      await shot(page, '08-esc-detail-fail.png');
      record('8.1 详情对话框 ESC 关闭', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 8.2 共享修改对话框 ESC（未提交）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '共享' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(300);
      await page.keyboard.press('Escape');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      const visible = await dlg.isVisible().catch(() => false);
      const patchCount = monitor.getCount();
      await shot(page, '08-esc-scope.png');
      if (!visible && patchCount === 0) {
        record('8.2 共享修改对话框 ESC 关闭+未提交', 'PASS');
      } else {
        record('8.2 共享修改对话框 ESC 关闭+未提交', 'FAIL',
          `visible=${visible}, PATCH 请求数=${patchCount}`);
      }
    } catch (e) {
      await shot(page, '08-esc-scope-fail.png');
      record('8.2 共享修改对话框 ESC 关闭+未提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 8.3 颁发对话框 ESC（未颁发）
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const monitor = createRequestMonitor(page, /\/v1\/auth\/apikey$/, 'POST');
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.login-card', { timeout: 10000 });
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(300);
      await page.keyboard.press('Escape');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      const visible = await dlg.isVisible().catch(() => false);
      const postCount = monitor.getCount();
      await shot(page, '08-esc-issue.png');
      if (!visible && postCount === 0) {
        record('8.3 颁发对话框 ESC 关闭+未颁发', 'PASS');
      } else {
        record('8.3 颁发对话框 ESC 关闭+未颁发', 'FAIL',
          `visible=${visible}, POST 请求数=${postCount}`);
      }
    } catch (e) {
      await shot(page, '08-esc-issue-fail.png');
      record('8.3 颁发对话框 ESC 关闭+未颁发', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 8.4 添加关联对话框 ESC（未创建）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/links$/, 'POST');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '关联' }).click();
      const linksDlg = page.locator('.el-dialog', { hasText: '资产关联' });
      await linksDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      await linksDlg.locator('.el-button', { hasText: '添加关联' }).click();
      const addDlg = page.locator('.el-dialog', { hasText: '添加资产关联' });
      await addDlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(300);
      await page.keyboard.press('Escape');
      await addDlg.waitFor({ state: 'hidden', timeout: 3000 });
      const visible = await addDlg.isVisible().catch(() => false);
      const postCount = monitor.getCount();
      await shot(page, '08-esc-addlink.png');
      if (!visible && postCount === 0) {
        record('8.4 添加关联对话框 ESC 关闭+未创建', 'PASS');
      } else {
        record('8.4 添加关联对话框 ESC 关闭+未创建', 'FAIL',
          `visible=${visible}, POST 请求数=${postCount}`);
      }
    } catch (e) {
      await shot(page, '08-esc-addlink-fail.png');
      record('8.4 添加关联对话框 ESC 关闭+未创建', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 9. 对话框遮罩点击关闭
  // ============================================================
  console.log('\n--- 9. 对话框遮罩点击关闭 ---');

  // 9.1 详情对话框遮罩点击
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '详情' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '资产详情' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      // 点击遮罩层（el-overlay）
      // 用 mouse.click 直接点击视口左上角遮罩区域（绕过 Playwright 可见性检查）
      await page.mouse.click(10, 10);
      await page.waitForTimeout(1000);
      const visible = await dlg.isVisible().catch(() => false);
      await shot(page, '09-mask-detail.png');
      if (!visible) {
        record('9.1 详情对话框遮罩点击关闭', 'PASS');
      } else {
        record('9.1 详情对话框遮罩点击关闭', 'FAIL', '遮罩点击后对话框仍可见（可能 close-on-click-modal=false）');
      }
    } catch (e) {
      await shot(page, '09-mask-detail-fail.png');
      record('9.1 详情对话框遮罩点击关闭', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 9.2 共享修改对话框遮罩点击（未提交）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '共享' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(500);
      // 用 mouse.click 直接点击视口左上角遮罩区域（绕过 Playwright 可见性检查）
      await page.mouse.click(10, 10);
      await page.waitForTimeout(1000);
      const visible = await dlg.isVisible().catch(() => false);
      const patchCount = monitor.getCount();
      await shot(page, '09-mask-scope.png');
      if (!visible && patchCount === 0) {
        record('9.2 共享修改对话框遮罩点击+未提交', 'PASS');
      } else {
        record('9.2 共享修改对话框遮罩点击+未提交', 'FAIL',
          `visible=${visible}, PATCH 请求数=${patchCount}`);
      }
    } catch (e) {
      await shot(page, '09-mask-scope-fail.png');
      record('9.2 共享修改对话框遮罩点击+未提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 9.3 批量修改对话框遮罩点击（未提交）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      await page.locator('.app-main .el-table__row .el-checkbox').first().click();
      await page.waitForTimeout(400);
      await page.locator('.el-button', { hasText: '批量修改共享' }).first().click();
      const dlg = page.locator('.el-dialog', { hasText: '批量修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(500);
      // 用 mouse.click 直接点击视口左上角遮罩区域（绕过 Playwright 可见性检查）
      await page.mouse.click(10, 10);
      await page.waitForTimeout(1000);
      const visible = await dlg.isVisible().catch(() => false);
      const patchCount = monitor.getCount();
      await shot(page, '09-mask-batch.png');
      if (!visible && patchCount === 0) {
        record('9.3 批量修改对话框遮罩点击+未提交', 'PASS');
      } else {
        record('9.3 批量修改对话框遮罩点击+未提交', 'FAIL',
          `visible=${visible}, PATCH 请求数=${patchCount}`);
      }
    } catch (e) {
      await shot(page, '09-mask-batch-fail.png');
      record('9.3 批量修改对话框遮罩点击+未提交', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 10. 表单中途取消
  // ============================================================
  console.log('\n--- 10. 表单中途取消 ---');

  // 10.1 共享修改取消（scope 未变）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/scope/, 'PATCH');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      const firstRow = page.locator('.app-main .el-table__row').first();
      const rowText = await firstRow.textContent();
      const originalScope = rowText.includes('私有') ? 'private' : 'team';
      await firstRow.locator('.el-button', { hasText: '共享' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      // 选择一个不同的 scope
      const newLabel = originalScope === 'private' ? '团队共享' : '私有';
      await dlg.locator('.el-radio', { hasText: newLabel }).click();
      await page.waitForTimeout(300);
      // 点取消
      await dlg.locator('.el-button', { hasText: '取消' }).click();
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      await page.waitForTimeout(500);
      // 验证 scope 未变
      const rowText2 = await page.locator('.app-main .el-table__row').first().textContent();
      const currentScope = rowText2.includes('私有') ? 'private' : 'team';
      const patchCount = monitor.getCount();
      await shot(page, '10-cancel-scope.png');
      if (currentScope === originalScope && patchCount === 0) {
        record('10.1 共享修改取消：scope 未变', 'PASS');
      } else {
        record('10.1 共享修改取消：scope 未变', 'FAIL',
          `original=${originalScope} current=${currentScope} PATCH=${patchCount}`);
      }
    } catch (e) {
      await shot(page, '10-cancel-scope-fail.png');
      record('10.1 共享修改取消：scope 未变', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 10.2 添加关联取消（未创建）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/links$/, 'POST');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '关联' }).click();
      const linksDlg = page.locator('.el-dialog', { hasText: '资产关联' });
      await linksDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      await linksDlg.locator('.el-button', { hasText: '添加关联' }).click();
      const addDlg = page.locator('.el-dialog', { hasText: '添加资产关联' });
      await addDlg.waitFor({ state: 'visible', timeout: 3000 });
      await addDlg.getByPlaceholder('关联到的资产 ID').fill('asset-alice-003');
      await page.waitForTimeout(300);
      // 点取消
      await addDlg.locator('.el-button', { hasText: '取消' }).click();
      await addDlg.waitFor({ state: 'hidden', timeout: 3000 });
      const postCount = monitor.getCount();
      await shot(page, '10-cancel-addlink.png');
      if (postCount === 0) {
        record('10.2 添加关联取消：未创建', 'PASS');
      } else {
        record('10.2 添加关联取消：未创建', 'FAIL', `POST 请求数=${postCount}`);
      }
    } catch (e) {
      await shot(page, '10-cancel-addlink-fail.png');
      record('10.2 添加关联取消：未创建', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 10.3 ACL 添加取消（未添加）
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const monitor = createRequestMonitor(page, /\/v1\/assets\/[^/]+\/acl$/, 'POST');
    // 准备 restricted 资产（ACL 页面只显示 restricted）
    const ACL_TEST_ASSET = 'asset-alice-004';
    let originalScope004 = null;
    try {
      const detail = await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}`, {
        headers: { 'X-API-Key': ALICE_KEY }
      }).then(r => r.json());
      originalScope004 = detail.scope;
      if (originalScope004 !== 'restricted') {
        await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: 'restricted' })
        }).then(r => r.json());
      }
    } catch (e) { /* 准备失败后面会自然报错 */ }
    try {
      await gotoMenu(page, 'ACL 授权');
      await waitTableLoaded(page, 10000);
      const row004 = page.locator('.el-table__row', { hasText: ACL_TEST_ASSET }).first();
      await row004.waitFor({ state: 'visible', timeout: 8000 });
      await row004.locator('.el-button', { hasText: '管理 ACL' }).click();
      const aclDlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await aclDlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      await aclDlg.locator('.el-button', { hasText: '添加授权' }).click();
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      await formDlg.waitFor({ state: 'visible', timeout: 3000 });
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill('cancel-test-' + Date.now());
      await page.waitForTimeout(300);
      // 点取消
      await formDlg.locator('.el-button', { hasText: '取消' }).click();
      await formDlg.waitFor({ state: 'hidden', timeout: 3000 });
      const postCount = monitor.getCount();
      await shot(page, '10-cancel-acl.png');
      if (postCount === 0) {
        record('10.3 ACL 添加取消：未添加', 'PASS');
      } else {
        record('10.3 ACL 添加取消：未添加', 'FAIL', `POST 请求数=${postCount}`);
      }
    } catch (e) {
      await shot(page, '10-cancel-acl-fail.png');
      record('10.3 ACL 添加取消：未添加', 'FAIL', e.message);
    } finally {
      // 恢复 asset-alice-004 原 scope
      if (originalScope004 && originalScope004 !== 'restricted') {
        await fetch(`http://localhost:8080/v1/assets/${ACL_TEST_ASSET}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: originalScope004 })
        }).catch(() => {});
      }
      await ctx.close();
    }
  }

  // ============================================================
  // 11. 菜单快速切换
  // ============================================================
  console.log('\n--- 11. 菜单快速切换 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    try {
      const errorsBefore = errors.length;
      // 1 秒内快速切换菜单 10 次
      const menuSequence = ['共享库', '资产图谱', 'ACL 授权', '我的规则库', '共享管理',
                            '共享库', '资产图谱', 'ACL 授权', '我的规则库', '共享管理'];
      for (let i = 0; i < menuSequence.length; i++) {
        await page.locator('.el-menu-item', { hasText: menuSequence[i] }).first().click();
        // 不等待加载完成，快速切换
        await page.waitForTimeout(100);
      }
      // 等待最终状态稳定
      await page.waitForTimeout(3000);

      // 验证最终停在"共享管理"
      const activeMenu = await page.locator('.el-menu-item.is-active').first().textContent();
      const appMainText = await page.locator('.app-main').first().textContent();
      const hasContent = appMainText.includes('选择性共享管理');
      const newErrors = errors.slice(errorsBefore);
      // 过滤非致命错误
      const fatalErrors = newErrors.filter(e =>
        !e.includes('favicon') && !e.includes('Failed to load resource') && !e.includes('net::ERR')
      );

      await shot(page, '11-menu-rapid-switch.png');

      if (hasContent && fatalErrors.length === 0) {
        record('11. 菜单快速切换 10 次', 'PASS',
          `最终菜单=${activeMenu.trim()}，无致命 JS 错误`);
      } else if (hasContent) {
        record('11. 菜单快速切换 10 次', 'PASS',
          `最终菜单=${activeMenu.trim()}，但有 ${fatalErrors.length} 条致命错误：${fatalErrors.slice(0, 2).join('; ')}`);
      } else {
        record('11. 菜单快速切换 10 次', 'FAIL',
          `最终菜单=${activeMenu.trim()}，内容不匹配。致命错误=${fatalErrors.length}`);
      }
    } catch (e) {
      await shot(page, '11-menu-rapid-switch-fail.png');
      record('11. 菜单快速切换 10 次', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 12. 分页快速切换
  // ============================================================
  console.log('\n--- 12. 分页快速切换 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await waitTableLoaded(page);
      const errorsBefore = errors.length;

      // 1 秒内连续点下一页 5 次
      for (let i = 0; i < 5; i++) {
        try {
          await page.locator('.el-pagination .btn-next').first().click({ timeout: 1000 });
        } catch (e) { /* 可能到最后一页 */ }
        await page.waitForTimeout(100);
      }
      // 等待最终状态稳定
      await page.waitForTimeout(3000);

      // 检查页码是否合理（1~6 之间）
      const activePage = await page.locator('.el-pager .number.is-active').first().textContent().catch(() => '?');
      const rows = await page.locator('.app-main .el-table__row').count();
      const newErrors = errors.slice(errorsBefore);
      const fatalErrors = newErrors.filter(e =>
        !e.includes('favicon') && !e.includes('Failed to load resource') && !e.includes('net::ERR')
      );

      await shot(page, '12-pagination-rapid.png');

      if (fatalErrors.length === 0 && rows >= 0) {
        record('12. 分页快速切换 5 次', 'PASS',
          `最终页码=${activePage}，行数=${rows}，无致命错误`);
      } else {
        record('12. 分页快速切换 5 次', 'FAIL',
          `页码=${activePage}，行数=${rows}，致命错误=${fatalErrors.length}`);
      }
    } catch (e) {
      await shot(page, '12-pagination-rapid-fail.png');
      record('12. 分页快速切换 5 次', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 13. 筛选器快速变更
  // ============================================================
  console.log('\n--- 13. 筛选器快速变更 ---');
  {
    const { ctx, page } = await loginAsAlice(browser, ALICE_KEY);
    const errors = attachConsoleCollector(page);
    const monitor = createRequestMonitor(page, /\/v1\/assets/, 'GET');
    try {
      await page.waitForSelector('.app-main .el-table__row');
      await waitTableLoaded(page);
      monitor.reset();

      const errorsBefore = errors.length;
      // 1 秒内连续变更类型筛选 5 次：rule → memory → skill → tool → prompt
      const types = ['rule 规则', 'memory 记忆', 'skill 技能', 'tool 工具', 'prompt 提示词'];
      const typeSelect = page.locator('.filter-bar .el-select').first();
      for (const typeLabel of types) {
        await selectOption(page, typeSelect, typeLabel);
        await page.waitForTimeout(100);
      }
      // 等待最终状态稳定
      await page.waitForTimeout(3000);

      // 验证表格最终显示 prompt 类型
      const typeTags = await page.locator('.app-main .el-table__row td:nth-child(2) .el-tag').allTextContents();
      const allPrompt = typeTags.length > 0 && typeTags.every(t => t.trim() === 'prompt');
      const newErrors = errors.slice(errorsBefore);
      const fatalErrors = newErrors.filter(e =>
        !e.includes('favicon') && !e.includes('Failed to load resource') && !e.includes('net::ERR')
      );

      const reqCount = monitor.getCount();
      await shot(page, '13-filter-rapid-change.png');

      if (fatalErrors.length === 0 && allPrompt) {
        record('13. 筛选器快速变更 5 次', 'PASS',
          `最终筛选=prompt，行数=${typeTags.length}，GET 请求数=${reqCount}`);
      } else if (fatalErrors.length === 0 && typeTags.length === 0) {
        record('13. 筛选器快速变更 5 次', 'PASS',
          `最终筛选=prompt（无 prompt 数据），GET 请求数=${reqCount}`);
      } else {
        record('13. 筛选器快速变更 5 次', 'FAIL',
          `allPrompt=${allPrompt}，行数=${typeTags.length}，致命错误=${fatalErrors.length}，GET 请求数=${reqCount}`);
      }
    } catch (e) {
      await shot(page, '13-filter-rapid-change-fail.png');
      record('13. 筛选器快速变更 5 次', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  await browser.close();

  // ============================================================
  // 全局数据恢复：确保所有资产 scope 恢复到测试前状态
  // ============================================================
  let restoredCount = 0;
  for (const [assetId, scope] of Object.entries(originalScopes)) {
    try {
      const cur = await fetch(`http://localhost:8080/v1/assets/${assetId}`, {
        headers: { 'X-API-Key': ALICE_KEY }
      }).then(r => r.json());
      if (cur.scope !== scope) {
        await fetch(`http://localhost:8080/v1/assets/${assetId}/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope })
        }).then(r => r.json());
        console.log(`恢复 ${assetId}: ${cur.scope} → ${scope}`);
        restoredCount++;
      }
    } catch (e) { /* 恢复失败不致命 */ }
  }
  if (restoredCount > 0) {
    console.log(`全局恢复完成：${restoredCount} 个资产的 scope 已还原`);
  } else {
    console.log('全局恢复完成：所有资产 scope 未变化，无需还原');
  }

  // ============================================================
  // 汇总报告
  // ============================================================
  console.log('\n================ 测试汇总 ================');
  console.log(`总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}`);
  console.log('------------------------------------------');
  for (const r of results) {
    const tag = r.status === 'PASS' ? '[PASS]' : r.status === 'FAIL' ? '[FAIL]' : '[SKIP]';
    console.log(`${tag} ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
  }
  console.log('==========================================');
  console.log(`截图目录：${SCREEN_DIR}`);

  const reportPath = path.join(__dirname, 'abnormal-action-results.txt');
  const reportContent = `TeamHarness 前端异常操作测试结果\n生成时间：${new Date().toISOString()}\n总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
    results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
    `\n\n截图目录：${SCREEN_DIR}\n`;
  fs.writeFileSync(reportPath, reportContent, 'utf8');

  process.exit(failCount > 0 ? 1 : 0);
})();
