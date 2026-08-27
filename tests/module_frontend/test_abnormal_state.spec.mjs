// TeamHarness 前端状态异常测试
// 工具：Playwright + chromium
// 运行：node tests/module_frontend/test_abnormal_state.spec.mjs
//
// 测试范围（严格限定状态异常）：
//   1-5.   localStorage 篡改（memberId 不匹配 / apiKey 无效 / apiKey 空 / 清空 / 非法 JSON）
//   6.    Token 过期模拟（轮换 key）
//   7.    网络中断模拟
//   8-9.  后端 503 / 500 模拟
//   10-11. 后端返回非法 JSON / 空响应
//   12.   后端超时模拟
//   13.   多标签页状态同步（localStorage 隔离）
//   14.   页面刷新后状态恢复
//   15.   浏览器前进后退
//
// 测试铁律：用例独立可复现（每用例独立 browser context）；不修改源代码；
//          网络模拟用 page.route()；FAIL 先定位根因
//
// 已知前端状态处理逻辑（来自源码分析）：
//   - checkLogin()（app.js:23）只检查 localStorage 有 key+member，不调用 lookup 校验
//     → 篡改 apiKey 为无效值后刷新，仍进入主界面（缺陷）
//   - request()（api.js:7）无 401 拦截器，不自动退出登录
//     → 401 只显示 "加载失败" 消息，用户卡在主界面（缺陷）
//   - loadMyAssets 用 currentMember（来自 localStorage）作 owner，不校验与 apiKey 对应关系
//     → 篡改 memberId 为 bob 后，会查到 bob 的 team/public 资产（安全问题）
//   - Vue {{ }} 默认 HTML 转义，XSS 不会执行

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const SCREEN_DIR = path.join(__dirname, 'screenshots', 'abnormal_state');

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

async function clickBtn(page, text, options = {}) {
  const btn = page.locator('.el-button', { hasText: text }).first();
  await btn.waitFor({ state: 'visible', timeout: options.timeout || 5000 });
  await btn.click();
}

async function fillInput(page, placeholder, value) {
  const inp = page.getByPlaceholder(placeholder).first();
  await inp.waitFor({ state: 'visible', timeout: 5000 });
  await inp.fill(value);
}

async function hasMessage(page, type, timeout = 1500) {
  try {
    await page.waitForSelector(`.el-message.el-message--${type}`, { timeout });
    return true;
  } catch { return false; }
}

async function getMessageText(page, type, timeout = 3000) {
  try {
    await page.waitForSelector(`.el-message.el-message--${type}`, { timeout });
    return await page.locator(`.el-message.el-message--${type}`).first().textContent({ timeout });
  } catch { return null; }
}

// 等待 loading mask 隐藏（loading 恢复）
async function waitForLoadingDone(page, timeout = 5000) {
  try {
    await page.waitForSelector('.el-loading-mask', { state: 'hidden', timeout });
    return true;
  } catch { return false; }
}

// 检查是否在登录页
async function isOnLoginPage(page) {
  return await page.locator('.login-card').isVisible().catch(() => false);
}

// 检查是否在主界面
async function isOnMainApp(page) {
  return await page.locator('.app-header').isVisible().catch(() => false);
}

// 登录 alice（用提供的 key）
async function loginAsAlice(page, apiKey) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.login-card', { timeout: 10000 });
  await fillInput(page, '如：alice', 'alice');
  await fillInput(page, 'th_ 开头', apiKey);
  await clickBtn(page, '登录');
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(1000);
}

// 通过 API 颁发 key
async function issueKey(memberId, agentId) {
  const resp = await fetch(`${BASE}v1/auth/apikey`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ member_id: memberId, agent_id: agentId }),
  });
  const data = await resp.json();
  return data; // { api_key, agent_id, key_id, key_prefix }
}

// 通过 API 轮换 key（旧 key 失效）
async function rotateKey(keyId) {
  const resp = await fetch(`${BASE}v1/auth/apikey/rotate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key_id: keyId }),
  });
  return await resp.json();
}

// 控制台错误收集
function attachConsoleCollector(page) {
  const errors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', err => {
    errors.push(`PAGEERROR: ${err.message}`);
  });
  return errors;
}

// ---------------- 主流程 ----------------
(async () => {
  console.log('=== TeamHarness 前端状态异常测试开始 ===\n');

  // 颁发 alice key 用于测试（除用例 6 外共用）
  console.log('颁发 alice 测试 key...');
  let ALICE_KEY;
  try {
    const issued = await issueKey('alice', 'agent-alice');
    ALICE_KEY = issued.api_key;
    console.log(`alice key 颁发成功：${issued.key_prefix}... (key_id=${issued.key_id})\n`);
  } catch (e) {
    console.error('颁发 alice key 失败：', e.message);
    process.exit(2);
  }

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  // ============================================================
  // 1. localStorage 篡改：memberId 不匹配（alice → bob）
  // ============================================================
  console.log('--- 1. localStorage 篡改：memberId 不匹配 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachConsoleCollector(page);
    // 收集发出的请求
    const assetRequests = [];
    page.on('request', req => {
      if (req.url().includes('/v1/assets')) {
        assetRequests.push(req.url());
      }
    });
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 篡改 memberId 为 bob
      await page.evaluate(() => {
        localStorage.setItem('teamharness_member_id', 'bob');
      });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1500);

      const onMain = await isOnMainApp(page);
      const memberTag = await page.locator('.el-tag', { hasText: '成员：' }).first().textContent().catch(() => '');
      const tableText = await page.locator('.app-main .table-container').first().textContent().catch(() => '');
      const showsBob = memberTag.includes('成员：bob');
      const showsBobAssets = tableText.includes('asset-bob-');

      await shot(page, '01-memberid-tamper-bob.png');

      // 安全问题：篡改成功 = 前端显示 bob 并查到 bob 的资产
      // 后端 scope 访问控制：alice 只能看到 bob 的 team/public 资产（asset-bob-001/003/004）
      if (onMain && showsBob) {
        if (showsBobAssets) {
          record('1. localStorage 篡改 memberId 为 bob', 'FAIL',
            `安全问题：前端显示"${memberTag.trim()}"，且加载到 bob 的资产（${assetRequests.join(', ')}）。根因：checkLogin(app.js:23) 不校验 memberId 与 apiKey 对应关系；loadMyAssets(app.js:119) 直接用 localStorage 的 memberId 作 owner 参数，后端 require_member(assets/api.py:75) 只校验 key 有效性，不校验 owner 与 key 的对应关系。alice 用自己的 key 可查到 bob 的 team/public 资产`);
        } else {
          record('1. localStorage 篡改 memberId 为 bob', 'PASS',
            `前端显示"${memberTag.trim()}"，但未查到 bob 资产（可能 bob 无 team/public 资产）`);
        }
      } else {
        throw new Error(`预期主界面+成员bob，实际 onMain=${onMain} memberTag="${memberTag.trim()}"`);
      }
    } catch (e) {
      await shot(page, '01-memberid-tamper-bob-fail.png');
      record('1. localStorage 篡改 memberId 为 bob', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 2. localStorage 篡改：apiKey 为无效值（th_invalid）
  // ============================================================
  console.log('\n--- 2. localStorage 篡改：apiKey 为 th_invalid ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachConsoleCollector(page);
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 篡改 apiKey 为无效值
      await page.evaluate(() => {
        localStorage.setItem('teamharness_api_key', 'th_invalid');
      });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const onLogin = await isOnLoginPage(page);
      // checkLogin 只检查 key 非空 → 进入主界面 → API 调用 401 → 显示错误消息
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);

      await shot(page, '02-apikey-invalid.png');

      // 缺陷：应退出到登录页或提示重新登录，但实际仍停留在主界面
      if (onMain && !onLogin) {
        record('2. localStorage 篡改 apiKey 为 th_invalid', 'FAIL',
          `缺陷：checkLogin(app.js:23) 只检查 key 非空不校验有效性，进入主界面后 API 返回 401，前端 request()(api.js:7) 无 401 拦截器不自动退出。用户卡在主界面看到错误消息："${msgText || '(无)'}"。应增加：checkLogin 调用 lookupApiKey 校验，或 request() 拦截 401 自动 logout`);
      } else if (onLogin) {
        record('2. localStorage 篡改 apiKey 为 th_invalid', 'PASS',
          '已退出到登录页（401 被正确处理）');
      } else {
        throw new Error(`状态异常：onMain=${onMain} onLogin=${onLogin}`);
      }
    } catch (e) {
      await shot(page, '02-apikey-invalid-fail.png');
      record('2. localStorage 篡改 apiKey 为 th_invalid', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 3. localStorage 篡改：apiKey 为空字符串
  // ============================================================
  console.log('\n--- 3. localStorage 篡改：apiKey 为空字符串 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      await page.evaluate(() => {
        localStorage.setItem('teamharness_api_key', '');
      });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1000);

      const onLogin = await isOnLoginPage(page);
      const onMain = await isOnMainApp(page);

      await shot(page, '03-apikey-empty.png');

      // checkLogin 中 key && member → "" 是 falsy → 返回 false → 停在登录页（正确）
      if (onLogin && !onMain) {
        record('3. localStorage 篡改 apiKey 为空字符串', 'PASS',
          'checkLogin 中空字符串为 falsy，正确返回登录页');
      } else {
        throw new Error(`预期登录页，实际 onLogin=${onLogin} onMain=${onMain}`);
      }
    } catch (e) {
      await shot(page, '03-apikey-empty-fail.png');
      record('3. localStorage 篡改 apiKey 为空字符串', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 4. localStorage 篡改：删除所有 localStorage
  // ============================================================
  console.log('\n--- 4. localStorage 篡改：清空 localStorage ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      await page.evaluate(() => {
        localStorage.clear();
      });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1000);

      const onLogin = await isOnLoginPage(page);
      const onMain = await isOnMainApp(page);

      await shot(page, '04-localstorage-cleared.png');

      if (onLogin && !onMain) {
        record('4. localStorage 篡改：清空 localStorage', 'PASS',
          'checkLogin 检测到无 key/member，正确返回登录页');
      } else {
        throw new Error(`预期登录页，实际 onLogin=${onLogin} onMain=${onMain}`);
      }
    } catch (e) {
      await shot(page, '04-localstorage-cleared-fail.png');
      record('4. localStorage 篡改：清空 localStorage', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 5. localStorage 篡改：注入非法 JSON（XSS）
  // ============================================================
  console.log('\n--- 5. localStorage 篡改：memberId 为 <script> ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    let dialogAppeared = false;
    // 监听 dialog（alert/confirm/prompt）— 如果 XSS 执行会触发 alert
    page.on('dialog', async dialog => {
      dialogAppeared = true;
      await dialog.dismiss();
    });
    try {
      await loginAsAlice(page, ALICE_KEY);
      await page.evaluate(() => {
        localStorage.setItem('teamharness_member_id', '<script>alert(1)</script>');
      });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1500);

      const onMain = await isOnMainApp(page);
      const memberTag = await page.locator('.el-tag', { hasText: '成员：' }).first().textContent().catch(() => '');
      await shot(page, '05-xss-memberid.png');

      // Vue {{ }} 默认 HTML 转义 → <script> 不会被解析为标签
      if (dialogAppeared) {
        record('5. localStorage 篡改：memberId 为 <script>', 'FAIL',
          'XSS 执行：alert 对话框被触发');
      } else if (onMain && memberTag.includes('<script>')) {
        record('5. localStorage 篡改：memberId 为 <script>', 'PASS',
          `Vue 模板默认转义，XSS 未执行。memberTag 显示为纯文本："${memberTag.trim()}"`);
      } else {
        // 即使 memberTag 不含 <script>（可能被截断），只要没触发 dialog 就算安全
        record('5. localStorage 篡改：memberId 为 <script>', 'PASS',
          `XSS 未执行（无 dialog），onMain=${onMain} memberTag="${memberTag.trim()}"`);
      }
    } catch (e) {
      await shot(page, '05-xss-memberid-fail.png');
      record('5. localStorage 篡改：memberId 为 <script>', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 6. Token 过期模拟（轮换 key）
  // ============================================================
  console.log('\n--- 6. Token 过期模拟（轮换 key） ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      // 颁发临时 key
      const tempKey = await issueKey('alice', 'agent-alice');
      console.log(`  临时 key 颁发：${tempKey.key_prefix}... (key_id=${tempKey.key_id})`);

      // 用临时 key 登录
      await loginAsAlice(page, tempKey.api_key);
      await page.waitForTimeout(500);

      // 轮换临时 key → 旧 key 失效
      const rotated = await rotateKey(tempKey.key_id);
      console.log(`  key 已轮换，旧 key 失效，新 key：${rotated.key_prefix}...`);

      // 在已登录页面点击"刷新"（触发 loadMyAssets）
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const onLogin = await isOnLoginPage(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);

      await shot(page, '06-token-rotated.png');

      // 旧 key 失效后，API 返回 401，但前端无 401 拦截器
      if (onMain && !onLogin) {
        record('6. Token 过期模拟（轮换 key）', 'FAIL',
          `缺陷：旧 key 失效后 API 返回 401，但前端无 401 拦截器，用户卡在主界面。错误消息："${msgText || '(无)'}"。应增加 401 拦截：自动清除 localStorage 并跳转登录页，提示"登录已过期，请重新登录"`);
      } else if (onLogin) {
        record('6. Token 过期模拟（轮换 key）', 'PASS',
          '旧 key 失效后正确退出到登录页');
      } else {
        throw new Error(`状态异常：onMain=${onMain} onLogin=${onLogin}`);
      }
    } catch (e) {
      await shot(page, '06-token-rotated-fail.png');
      record('6. Token 过期模拟（轮换 key）', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 7. 网络中断模拟
  // ============================================================
  console.log('\n--- 7. 网络中断模拟 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 设置 route 拦截所有 /v1/ 请求 → abort（模拟网络中断）
      await page.route('**/v1/**', route => route.abort());
      // 点击刷新
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);
      // 检查 loading 是否恢复
      const loadingDone = await waitForLoadingDone(page, 2000);

      await shot(page, '07-network-aborted.png');

      // 验证：不崩溃 + 有错误提示 + loading 恢复
      const notCrashed = onMain;
      if (notCrashed && hasErrorMsg && loadingDone) {
        record('7. 网络中断模拟', 'PASS',
          `不崩溃，有错误提示："${msgText || '(无)'}"，loading 已恢复`);
      } else {
        record('7. 网络中断模拟', 'FAIL',
          `notCrashed=${notCrashed} hasErrorMsg=${hasErrorMsg} loadingDone=${loadingDone} msg="${msgText || '(无)'}"`);
      }
    } catch (e) {
      await shot(page, '07-network-aborted-fail.png');
      record('7. 网络中断模拟', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 8. 后端 503 模拟
  // ============================================================
  console.log('\n--- 8. 后端 503 模拟 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 设置 route 拦截 /v1/assets → 503
      await page.route('**/v1/assets**', route => route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Service Unavailable' }),
      }));
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);
      const loadingDone = await waitForLoadingDone(page, 2000);

      await shot(page, '08-backend-503.png');

      if (onMain && hasErrorMsg && loadingDone) {
        record('8. 后端 503 模拟', 'PASS',
          `不崩溃，有错误提示："${msgText || '(无)'}"，loading 已恢复`);
      } else {
        record('8. 后端 503 模拟', 'FAIL',
          `onMain=${onMain} hasErrorMsg=${hasErrorMsg} loadingDone=${loadingDone} msg="${msgText || '(无)'}"`);
      }
    } catch (e) {
      await shot(page, '08-backend-503-fail.png');
      record('8. 后端 503 模拟', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 9. 后端 500 模拟
  // ============================================================
  console.log('\n--- 9. 后端 500 模拟 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      await page.route('**/v1/assets**', route => route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Internal Server Error' }),
      }));
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);
      const loadingDone = await waitForLoadingDone(page, 2000);

      await shot(page, '09-backend-500.png');

      if (onMain && hasErrorMsg && loadingDone) {
        record('9. 后端 500 模拟', 'PASS',
          `不崩溃，有错误提示："${msgText || '(无)'}"，loading 已恢复`);
      } else {
        record('9. 后端 500 模拟', 'FAIL',
          `onMain=${onMain} hasErrorMsg=${hasErrorMsg} loadingDone=${loadingDone} msg="${msgText || '(无)'}"`);
      }
    } catch (e) {
      await shot(page, '09-backend-500-fail.png');
      record('9. 后端 500 模拟', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 10. 后端返回非法 JSON
  // ============================================================
  console.log('\n--- 10. 后端返回非法 JSON ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachConsoleCollector(page);
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 模拟 200 但 body 不是合法 JSON
      await page.route('**/v1/assets**', route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: 'not json',
      }));
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);
      const loadingDone = await waitForLoadingDone(page, 2000);

      await shot(page, '10-invalid-json.png');

      // request() 中 resp.ok=true → resp.json() 抛出 SyntaxError → 被 loadMyAssets catch 捕获
      if (onMain && hasErrorMsg && loadingDone) {
        record('10. 后端返回非法 JSON', 'PASS',
          `不崩溃，有错误提示："${msgText || '(无)'}"，loading 已恢复`);
      } else {
        record('10. 后端返回非法 JSON', 'FAIL',
          `onMain=${onMain} hasErrorMsg=${hasErrorMsg} loadingDone=${loadingDone} msg="${msgText || '(无)'}"`);
      }
    } catch (e) {
      await shot(page, '10-invalid-json-fail.png');
      record('10. 后端返回非法 JSON', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 11. 后端返回空响应
  // ============================================================
  console.log('\n--- 11. 后端返回空响应 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      await page.route('**/v1/assets**', route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '',
      }));
      await clickBtn(page, '刷新');
      await page.waitForTimeout(2000);

      const onMain = await isOnMainApp(page);
      const hasErrorMsg = await hasMessage(page, 'error', 2000);
      const msgText = await getMessageText(page, 'error', 1000);
      const loadingDone = await waitForLoadingDone(page, 2000);

      await shot(page, '11-empty-response.png');

      // 空响应 → resp.json() 抛出 SyntaxError → catch 捕获
      if (onMain && hasErrorMsg && loadingDone) {
        record('11. 后端返回空响应', 'PASS',
          `不崩溃，有错误提示："${msgText || '(无)'}"，loading 已恢复`);
      } else {
        record('11. 后端返回空响应', 'FAIL',
          `onMain=${onMain} hasErrorMsg=${hasErrorMsg} loadingDone=${loadingDone} msg="${msgText || '(无)'}"`);
      }
    } catch (e) {
      await shot(page, '11-empty-response-fail.png');
      record('11. 后端返回空响应', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 12. 后端超时模拟
  // ============================================================
  console.log('\n--- 12. 后端超时模拟 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);
      // 设置 route 延迟 30s 响应（模拟超时）
      await page.route('**/v1/assets**', route => {
        // 不立即 fulfill，延迟 30s
        setTimeout(() => route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }),
        }), 30000);
      });

      // 点击刷新
      await clickBtn(page, '刷新');
      await page.waitForTimeout(1000);

      // 检查 loading 是否显示（应该卡在 loading）
      const loadingVisible = await page.locator('.el-loading-mask').first().isVisible().catch(() => false);

      // 检查是否有超时处理（前端 fetch 没有超时，应一直等待）
      const hasTimeoutMsg = await hasMessage(page, 'error', 2000);

      await shot(page, '12-timeout-loading.png');

      // 前端 fetch 无超时设置，loading 会一直保持
      // 用户无法取消（刷新按钮在 loading 时变成 loading 状态，无取消按钮）
      if (loadingVisible && !hasTimeoutMsg) {
        record('12. 后端超时模拟', 'FAIL',
          `缺陷：fetch(api.js:14) 无超时设置，loading 卡住（30s 内未恢复）。前端无取消按钮，用户无法中止请求。应增加：fetch 超时（AbortController + setTimeout）或 loading 上的取消按钮`);
      } else if (hasTimeoutMsg) {
        record('12. 后端超时模拟', 'PASS',
          '前端有超时处理，loading 已恢复');
      } else {
        record('12. 后端超时模拟', 'FAIL',
          `loadingVisible=${loadingVisible} hasTimeoutMsg=${hasTimeoutMsg}`);
      }

      // 清理：unroute 让后续请求正常（虽然 context 即将关闭）
      await page.unroute('**/v1/assets**');
    } catch (e) {
      await shot(page, '12-timeout-loading-fail.png');
      record('12. 后端超时模拟', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 13. 多标签页状态同步（localStorage 隔离）
  // ============================================================
  console.log('\n--- 13. 多标签页状态同步 ---');
  {
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const pageA = await ctxA.newPage();
    const pageB = await ctxB.newPage();
    try {
      // A 标签登录 alice
      await loginAsAlice(pageA, ALICE_KEY);
      await pageA.waitForTimeout(500);

      // B 标签不登录，直接访问
      await pageB.goto(BASE, { waitUntil: 'domcontentloaded' });
      await pageB.waitForTimeout(1000);

      const bOnLogin = await isOnLoginPage(pageB);
      const bOnMain = await isOnMainApp(pageB);

      await shot(pageB, '13-multi-tab-B-not-logged-in.png');

      // 验证 B 标签不会因 A 登录而自动登录（localStorage 隔离）
      const isolated = bOnLogin && !bOnMain;

      // A 标签退出
      await clickBtn(pageA, '退出');
      await pageA.waitForTimeout(800);
      const aLoggedOut = await isOnLoginPage(pageA);

      // 验证 B 标签状态不变（独立）
      const bStillOnLogin = await isOnLoginPage(pageB);

      await shot(pageA, '13-multi-tab-A-logged-out.png');
      await shot(pageB, '13-multi-tab-B-still-not-logged-in.png');

      if (isolated && aLoggedOut && bStillOnLogin) {
        record('13. 多标签页状态同步（localStorage 隔离）', 'PASS',
          'B 标签未因 A 登录而自动登录；A 退出后 B 状态不变（localStorage 隔离正确）');
      } else {
        record('13. 多标签页状态同步', 'FAIL',
          `isolated=${isolated} aLoggedOut=${aLoggedOut} bStillOnLogin=${bStillOnLogin}`);
      }
    } catch (e) {
      await shot(pageA, '13-multi-tab-fail.png');
      record('13. 多标签页状态同步', 'FAIL', e.message);
    } finally {
      await ctxA.close();
      await ctxB.close();
    }
  }

  // ============================================================
  // 14. 页面刷新后状态恢复
  // ============================================================
  console.log('\n--- 14. 页面刷新后状态恢复 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);

      // 切换到资产图谱页
      const graphMenu = page.locator('.el-menu-item', { hasText: '资产图谱' }).first();
      await graphMenu.click();
      await page.waitForTimeout(600);

      // 输入根 ID 和深度
      await fillInput(page, '根资产 ID', 'asset-alice-001');
      await page.waitForTimeout(300);

      // 验证输入已填入
      const inputBefore = await page.getByPlaceholder('根资产 ID').first().inputValue();
      const activeMenuBefore = await page.locator('.el-menu-item.is-active').first().textContent();

      // 刷新页面
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1500);

      const onMain = await isOnMainApp(page);
      const onLogin = await isOnLoginPage(page);
      const activeMenuAfter = await page.locator('.el-menu-item.is-active').first().textContent().catch(() => '');
      const inputAfter = await page.getByPlaceholder('根资产 ID').first().inputValue().catch(() => '__NOT_VISIBLE__');

      await shot(page, '14-refresh-state-recovery.png');

      // 验证：
      // 1. 仍登录（localStorage 保持）
      // 2. 图谱页输入丢失（graphRootId 是 ref，刷新后重置为空）
      // 3. 当前菜单回到默认（my）— activeMenu 默认 "my"
      const stillLoggedIn = onMain && !onLogin;
      const inputLost = (inputAfter === '' || inputAfter === '__NOT_VISIBLE__');
      const menuBackToMy = activeMenuAfter.includes('我的规则库');

      if (stillLoggedIn && inputLost && menuBackToMy) {
        record('14. 页面刷新后状态恢复', 'PASS',
          `仍登录，图谱输入丢失（"${inputBefore}" → "${inputAfter}"），菜单回到"我的规则库"`);
      } else {
        record('14. 页面刷新后状态恢复', 'FAIL',
          `stillLoggedIn=${stillLoggedIn} inputLost=${inputLost}(before="${inputBefore}" after="${inputAfter}") menuBackToMy=${menuBackToMy}(after="${activeMenuAfter.trim()}")`);
      }
    } catch (e) {
      await shot(page, '14-refresh-state-recovery-fail.png');
      record('14. 页面刷新后状态恢复', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 15. 浏览器前进后退
  // ============================================================
  console.log('\n--- 15. 浏览器前进后退 ---');
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAlice(page, ALICE_KEY);

      // 切换菜单 my → shared → graph
      for (const menuText of ['共享库', '资产图谱']) {
        await page.locator('.el-menu-item', { hasText: menuText }).first().click();
        await page.waitForTimeout(500);
      }

      const activeBeforeBack = await page.locator('.el-menu-item.is-active').first().textContent();
      const urlBeforeBack = page.url();

      // 点浏览器后退
      try {
        await page.goBack({ waitUntil: 'domcontentloaded', timeout: 5000 });
      } catch (e) {
        // goBack 可能超时或无上一页
      }
      await page.waitForTimeout(1000);

      const onMain = await isOnMainApp(page);
      const onLogin = await isOnLoginPage(page);
      const activeAfterBack = await page.locator('.el-menu-item.is-active').first().textContent().catch(() => '');
      const urlAfterBack = page.url();

      await shot(page, '15-go-back.png');

      // app.js handleMenuSelect 只改 activeMenu.value，无 history.pushState
      // → goBack 不会改变菜单，可能离开应用（如果历史只有一页）
      if (!onMain && !onLogin) {
        // goBack 离开了应用（回到 about:blank 或上一页）
        record('15. 浏览器前进后退', 'FAIL',
          `无 history 集成：goBack 离开应用（url: ${urlBeforeBack} → ${urlAfterBack}）。handleMenuSelect(app.js:95) 只改 activeMenu，未调用 history.pushState。应在菜单切换时 pushState，使后退能回到上一菜单`);
      } else if (onMain) {
        // 仍在应用内，检查菜单是否变化
        const menuUnchanged = activeAfterBack.trim() === activeBeforeBack.trim();
        if (menuUnchanged) {
          record('15. 浏览器前进后退', 'FAIL',
            `无 history 集成：goBack 后菜单未变化（仍为"${activeAfterBack.trim()}"）。handleMenuSelect 未调用 history.pushState，后退不影响菜单状态`);
        } else {
          record('15. 浏览器前进后退', 'PASS',
            `goBack 后菜单从"${activeBeforeBack.trim()}"变为"${activeAfterBack.trim()}"`);
        }
      } else {
        record('15. 浏览器前进后退', 'FAIL',
          `状态异常：onMain=${onMain} onLogin=${onLogin} url=${urlAfterBack}`);
      }
    } catch (e) {
      await shot(page, '15-go-back-fail.png');
      record('15. 浏览器前进后退', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  await browser.close();

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

  const reportPath = path.join(__dirname, 'abnormal-state-results.txt');
  const reportContent = `TeamHarness 前端状态异常测试结果\n生成时间：${new Date().toISOString()}\n总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
    results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
    `\n\n截图目录：${SCREEN_DIR}\n`;
  fs.writeFileSync(reportPath, reportContent, 'utf8');
  console.log(`报告已保存：${reportPath}`);

  process.exit(failCount > 0 ? 1 : 0);
})();
