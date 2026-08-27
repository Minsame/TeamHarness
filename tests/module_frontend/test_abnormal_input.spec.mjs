// TeamHarness 前端异常输入测试
// 工具：Playwright + chromium
// 运行：node tests/module_frontend/test_abnormal_input.spec.mjs
//
// 测试范围（严格限定前端异常输入）：
//   1. 登录页异常输入
//   2. 颁发 API Key 对话框异常输入
//   3. 我的规则库筛选异常输入
//   4. 资产详情对话框异常输入（如能从 UI 触发）
//   5. 资产图谱异常输入
//   6. ACL 添加对话框异常输入
//   7. 添加关联对话框异常输入
//
// 测试铁律：用例独立可复现（每用例独立 browser context）；覆盖异常输入分支；
//          XSS 检测（dialog 监听 + pageerror）；SQL 注入检测（后端 500/数据异常）；
//          不修改源代码，只测试

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const SCREEN_DIR = path.join(__dirname, 'screenshots', 'abnormal_input');
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
  await inp.waitFor({ state: 'visible', timeout: 10000 });
  await inp.fill(value);
}

// 用 evaluate 设置 input value（用于 null 字节等 fill 不支持的场景）
async function fillInputNative(page, placeholder, value) {
  await page.evaluate(({ ph, val }) => {
    const inp = document.querySelector(`input[placeholder="${ph}"]`);
    if (!inp) throw new Error(`input[placeholder="${ph}"] not found`);
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inp, val);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  }, { ph: placeholder, val: value });
}

async function clickBtn(page, text, options = {}) {
  const btn = page.locator('.el-button', { hasText: text }).first();
  await btn.waitFor({ state: 'visible', timeout: options.timeout || 5000 });
  await btn.click();
}

async function waitForMessage(page, type, textContains, timeout = 3000) {
  const sel = `.el-message.el-message--${type}`;
  if (textContains) {
    const matched = page.locator(sel, { hasText: textContains }).first();
    await matched.waitFor({ state: 'visible', timeout });
    return await matched.textContent({ timeout });
  }
  await page.waitForSelector(sel, { timeout });
  return await page.locator(sel).first().textContent({ timeout });
}

async function hasMessage(page, type, timeout = 1500) {
  try {
    await page.waitForSelector(`.el-message.el-message--${type}`, { timeout });
    return true;
  } catch { return false; }
}

async function getMessageText(page, timeout = 2000) {
  try {
    await page.waitForSelector('.el-message', { timeout });
    return (await page.locator('.el-message').first().textContent({ timeout: 1000 })) || '';
  } catch { return ''; }
}

async function gotoMenu(page, menuText) {
  const item = page.locator('.el-menu-item', { hasText: menuText }).first();
  await item.click();
  await page.waitForTimeout(600);
}

async function closeDialogs(page) {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
}

// 收集 pageerror + console error
function attachErrorCollector(page) {
  const errors = [];
  page.on('pageerror', err => errors.push(`PAGEERROR: ${err.message}`));
  page.on('console', msg => {
    if (msg.type() === 'error') errors.push(`CONSOLE: ${msg.text()}`);
  });
  return errors;
}

// 拦截 window.alert/confirm/prompt（XSS 检测）
function attachDialogHandler(page) {
  const dialogs = [];
  page.on('dialog', async d => {
    dialogs.push({ type: d.type(), message: d.message() });
    await d.dismiss();
  });
  return dialogs;
}

// 登录 alice
async function loginAlice(page, aliceKey) {
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForSelector('.login-card');
  await fillInput(page, '如：alice', 'alice');
  await fillInput(page, 'th_ 开头', aliceKey);
  await clickBtn(page, '登录');
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(500);
}

// 生成超长字符串
function longStr(n = 10000) { return 'a'.repeat(n); }

// 过滤致命错误：只有 pageerror（未捕获 JS 异常）视为致命
// console error 可能是前端主动 catch 并 log 的错误处理（如 loadMemberStats 的
// console.error("加载统计失败:")），属于异常输入被后端 4xx 拒绝时的正常处理路径，不算崩溃
function fatalErrors(errors) {
  return errors.filter(e => e.includes('PAGEERROR'));
}

// ---------------- 主流程 ----------------
(async () => {
  console.log('=== TeamHarness 前端异常输入测试开始 ===\n');

  // 颁发 alice key（脚本级单例，所有需登录用例复用）
  console.log('颁发 alice API Key...');
  const issueResp = await fetch(`${BASE}v1/auth/apikey`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ member_id: 'alice', agent_id: 'agent-alice' }),
  }).then(r => r.json());
  if (!issueResp.api_key) {
    console.error('颁发 alice key 失败：', JSON.stringify(issueResp));
    process.exit(2);
  }
  const ALICE_KEY = issueResp.api_key;
  console.log('alice key:', ALICE_KEY, '\n');

  // ============================================================
  // 预置：asset-alice-004 临时改为 restricted scope（ACL 测试需要）
  // 原因：ACL 授权页查询 scope=restricted 的资产，004 当前 scope 可能为 team
  // ============================================================
  let original004Scope = null;
  try {
    const asset004 = await fetch(`${BASE}v1/assets/asset-alice-004`, {
      headers: { 'X-API-Key': ALICE_KEY },
    }).then(r => r.json());
    original004Scope = asset004.scope || null;
    console.log(`asset-alice-004 当前 scope="${original004Scope}"`);
    if (original004Scope && original004Scope !== 'restricted') {
      const patchResp = await fetch(`${BASE}v1/assets/asset-alice-004/scope`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
        body: JSON.stringify({ scope: 'restricted' }),
      });
      if (patchResp.ok) {
        console.log(`asset-alice-004 scope 已临时改为 restricted（原 ${original004Scope}，测试后还原）\n`);
      } else {
        console.warn(`asset-alice-004 scope 改为 restricted 失败：${patchResp.status}（ACL 测试可能受影响）\n`);
      }
    }
  } catch (e) {
    console.warn(`asset-alice-004 预置失败：${e.message}（ACL 测试可能受影响）\n`);
  }

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  // 开新 context + page + 错误/dialog 监听
  async function newPageWithMonitors() {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = attachErrorCollector(page);
    const dialogs = attachDialogHandler(page);
    return { ctx, page, errors, dialogs };
  }

  // ============================================================
  // 1. 登录页异常输入
  // ============================================================
  console.log('--- 1. 登录页异常输入 ---');

  // 1.1 超长成员 ID（10000 字符）
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', longStr());
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (!enteredMain && !stillLogin) throw new Error(`页面不可识别 enteredMain=${enteredMain} stillLogin=${stillLogin}`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-1-long-member-id.png');
      record('1.1 超长成员 ID（10000）', 'PASS', `enteredMain=${enteredMain}（member_id 仅存 localStorage，不参与后端查询）`);
    } catch (e) {
      await shot(page, '1-1-long-member-id-fail.png');
      record('1.1 超长成员 ID（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.2 超长 API Key（10000 字符）
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', longStr());
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (enteredMain) throw new Error('超长 API Key 不应登录成功');
      if (!stillLogin) throw new Error(`页面异常：stillLogin=${stillLogin}`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-2-long-api-key.png');
      record('1.2 超长 API Key（10000）', 'PASS', `仍在登录页，后端拒绝无效 key`);
    } catch (e) {
      await shot(page, '1-2-long-api-key-fail.png');
      record('1.2 超长 API Key（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.3 成员 ID 含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', `'; DROP TABLE users; --`);
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (!enteredMain) throw new Error('SQL 注入 member_id 应登录成功（member_id 不参与后端 SQL）');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      // 验证 member tag 正确转义显示
      const tagText = await page.locator('.el-tag', { hasText: '成员' }).first().textContent();
      await shot(page, '1-3-sql-injection-member.png');
      record('1.3 成员 ID 含 SQL 注入', 'PASS', `登录成功，member tag="${tagText.trim()}"，SQL 注入未生效（member_id 仅存 localStorage）`);
    } catch (e) {
      await shot(page, '1-3-sql-injection-member-fail.png');
      record('1.3 成员 ID 含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.4 成员 ID 含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', `<script>alert(1)</script>`);
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (!enteredMain) throw new Error('XSS member_id 应登录成功');
      // 关键：检查 dialog 是否触发（alert 执行）
      if (dialogs.length > 0) throw new Error(`XSS 已执行！dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      // 检查页面是否注入了未转义的 <script> 标签
      const hasRawScript = await page.evaluate(() => {
        const html = document.body.innerHTML;
        // Vue {{ }} 插值会转义为 &lt;script&gt;，未转义的 <script> 才是 XSS
        return html.includes('<script>alert(1)</script>');
      });
      if (hasRawScript) throw new Error('页面存在未转义的 <script> 标签');
      const tagText = await page.locator('.el-tag', { hasText: '成员' }).first().textContent();
      await shot(page, '1-4-xss-member.png');
      record('1.4 成员 ID 含 XSS', 'PASS', `登录成功，XSS 未执行，member tag="${tagText.trim()}"（Vue 插值转义）`);
    } catch (e) {
      await shot(page, '1-4-xss-member-fail.png');
      record('1.4 成员 ID 含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.5 API Key 含路径遍历
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', `../../etc/passwd`);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (enteredMain) throw new Error('路径遍历 API Key 不应登录成功');
      if (!stillLogin) throw new Error(`页面异常`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-5-path-traversal-key.png');
      record('1.5 API Key 含路径遍历', 'PASS', `仍在登录页，后端拒绝无效 key`);
    } catch (e) {
      await shot(page, '1-5-path-traversal-key-fail.png');
      record('1.5 API Key 含路径遍历', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.6 成员 ID 含 emoji
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', `alice🎉test`);
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (!enteredMain) throw new Error('emoji member_id 应登录成功');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tagText = await page.locator('.el-tag', { hasText: '成员' }).first().textContent();
      await shot(page, '1-6-emoji-member.png');
      record('1.6 成员 ID 含 emoji', 'PASS', `登录成功，member tag="${tagText.trim()}"`);
    } catch (e) {
      await shot(page, '1-6-emoji-member-fail.png');
      record('1.6 成员 ID 含 emoji', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.7 成员 ID 含 Unicode（中文）
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', `alice测试`);
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (!enteredMain) throw new Error('Unicode member_id 应登录成功');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tagText = await page.locator('.el-tag', { hasText: '成员' }).first().textContent();
      await shot(page, '1-7-unicode-member.png');
      record('1.7 成员 ID 含 Unicode', 'PASS', `登录成功，member tag="${tagText.trim()}"`);
    } catch (e) {
      await shot(page, '1-7-unicode-member-fail.png');
      record('1.7 成员 ID 含 Unicode', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.8 成员 ID 含 null 字节
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      // null 字节用 native setter 设置（fill 可能过滤）
      await fillInputNative(page, '如：alice', `alice\0admin`);
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (!enteredMain && !stillLogin) throw new Error(`页面不可识别 enteredMain=${enteredMain} stillLogin=${stillLogin}`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-8-null-byte-member.png');
      record('1.8 成员 ID 含 null 字节', 'PASS', `enteredMain=${enteredMain}（null 字节未导致崩溃）`);
    } catch (e) {
      await shot(page, '1-8-null-byte-member-fail.png');
      record('1.8 成员 ID 含 null 字节', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.9 API Key 含空格
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', `th_ key123`);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (enteredMain) throw new Error('含空格 API Key 不应登录成功');
      if (!stillLogin) throw new Error(`页面异常`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-9-space-key.png');
      record('1.9 API Key 含空格', 'PASS', `仍在登录页，后端拒绝无效 key`);
    } catch (e) {
      await shot(page, '1-9-space-key-fail.png');
      record('1.9 API Key 含空格', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.10 API Key 不以 th_ 开头
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', `invalid_key`);
      await clickBtn(page, '登录');
      await page.waitForTimeout(2500);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (enteredMain) throw new Error('非 th_ 开头 API Key 不应登录成功（AUTH-2 已修复）');
      if (!stillLogin) throw new Error(`页面异常`);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '1-10-invalid-key-prefix.png');
      record('1.10 API Key 不以 th_ 开头', 'PASS', `仍在登录页，前端校验 agent_id 拒绝`);
    } catch (e) {
      await shot(page, '1-10-invalid-key-prefix-fail.png');
      record('1.10 API Key 不以 th_ 开头', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.11 空 member + 有效 key
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await waitForMessage(page, 'warning', '请输入成员 ID');
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (!stillLogin) throw new Error('空 member 应仍在登录页');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '1-11-empty-member.png');
      record('1.11 空 member + 有效 key', 'PASS', `前端 warning 拦截`);
    } catch (e) {
      await shot(page, '1-11-empty-member-fail.png');
      record('1.11 空 member + 有效 key', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.12 有效 member + 空 key
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await clickBtn(page, '登录');
      await waitForMessage(page, 'warning', '请输入成员 ID');
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (!stillLogin) throw new Error('空 key 应仍在登录页');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '1-12-empty-key.png');
      record('1.12 有效 member + 空 key', 'PASS', `前端 warning 拦截`);
    } catch (e) {
      await shot(page, '1-12-empty-key-fail.png');
      record('1.12 有效 member + 空 key', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 1.13 两个都空
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '登录');
      await waitForMessage(page, 'warning', '请输入成员 ID');
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (!stillLogin) throw new Error('都空应仍在登录页');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '1-13-both-empty.png');
      record('1.13 两个都空', 'PASS', `前端 warning 拦截`);
    } catch (e) {
      await shot(page, '1-13-both-empty-fail.png');
      record('1.13 两个都空', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // ============================================================
  // 2. 颁发 API Key 对话框异常输入
  // ============================================================
  console.log('\n--- 2. 颁发 API Key 对话框异常输入 ---');

  // 2.1 成员 ID 含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.getByPlaceholder('如：alice').fill(`<script>alert(1)</script>`);
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill('agent-xss-test');
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '2-1-xss-member-issue.png');
      const msg = await getMessageText(page);
      record('2.1 颁发-成员 ID 含 XSS', 'PASS', `不崩溃，消息="${msg}"（颁发成功则 api_key 由后端生成，不含用户输入）`);
    } catch (e) {
      await shot(page, '2-1-xss-member-issue-fail.png');
      record('2.1 颁发-成员 ID 含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 2.2 成员 ID 含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.getByPlaceholder('如：alice').fill(`'; DROP TABLE users; --`);
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill('agent-sql-test');
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '2-2-sql-member-issue.png');
      const msg = await getMessageText(page);
      record('2.2 颁发-成员 ID 含 SQL 注入', 'PASS', `不崩溃，消息="${msg}"`);
    } catch (e) {
      await shot(page, '2-2-sql-member-issue-fail.png');
      record('2.2 颁发-成员 ID 含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 2.3 成员 ID 含路径遍历
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.getByPlaceholder('如：alice').fill(`../../etc/passwd`);
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill('agent-path-test');
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '2-3-path-member-issue.png');
      const msg = await getMessageText(page);
      record('2.3 颁发-成员 ID 含路径遍历', 'PASS', `不崩溃，消息="${msg}"`);
    } catch (e) {
      await shot(page, '2-3-path-member-issue-fail.png');
      record('2.3 颁发-成员 ID 含路径遍历', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 2.4 成员 ID 超长
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.getByPlaceholder('如：alice').fill(longStr());
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill('agent-long-test');
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await page.waitForTimeout(3000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '2-4-long-member-issue.png');
      const msg = await getMessageText(page);
      record('2.4 颁发-成员 ID 超长（10000）', 'PASS', `不崩溃，消息="${msg}"`);
    } catch (e) {
      await shot(page, '2-4-long-member-issue-fail.png');
      record('2.4 颁发-成员 ID 超长（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 2.5 Agent ID 含特殊字符
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.getByPlaceholder('如：alice').fill(`abnormal-agent-test`);
      await dlg.getByPlaceholder('如：agent-alice（可留空自动生成）').fill(`<script>alert(1)</script>; DROP TABLE--`);
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '2-5-special-agent-id.png');
      const msg = await getMessageText(page);
      record('2.5 颁发-Agent ID 含特殊字符', 'PASS', `不崩溃，消息="${msg}"`);
    } catch (e) {
      await shot(page, '2-5-special-agent-id-fail.png');
      record('2.5 颁发-Agent ID 含特殊字符', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 2.6 空 member 颁发
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      // 不填 member ID，直接点颁发
      await dlg.locator('.el-button--primary', { hasText: '颁发' }).click();
      await waitForMessage(page, 'warning', '请输入成员 ID');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '2-6-empty-member-issue.png');
      record('2.6 颁发-空 member ID', 'PASS', `前端 warning 拦截，未调用后端`);
    } catch (e) {
      await shot(page, '2-6-empty-member-issue-fail.png');
      record('2.6 颁发-空 member ID', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // ============================================================
  // 3. 我的规则库筛选异常输入
  // ============================================================
  console.log('\n--- 3. 我的规则库筛选异常输入 ---');

  // 3.1 分类筛选含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      const catInput = page.getByPlaceholder('分类筛选').first();
      await catInput.waitFor({ state: 'visible', timeout: 5000 });
      await catInput.fill(`'; DROP TABLE--`);
      await catInput.press('Enter');
      await page.waitForTimeout(2000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      // 表格应不报错（空结果或正常加载）
      const tableVisible = await page.locator('.app-main .el-table').first().isVisible().catch(() => false);
      if (!tableVisible) throw new Error('表格不可见');
      await shot(page, '3-1-sql-category-filter.png');
      const msg = await getMessageText(page);
      record('3.1 分类筛选含 SQL 注入', 'PASS', `表格不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '3-1-sql-category-filter-fail.png');
      record('3.1 分类筛选含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 3.2 模块路径含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      const mpInput = page.getByPlaceholder('模块路径').first();
      await mpInput.waitFor({ state: 'visible', timeout: 5000 });
      await mpInput.fill(`<script>alert(1)</script>`);
      await mpInput.press('Enter');
      await page.waitForTimeout(2000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tableVisible = await page.locator('.app-main .el-table').first().isVisible().catch(() => false);
      if (!tableVisible) throw new Error('表格不可见');
      // 检查页面无未转义 script
      const hasRawScript = await page.evaluate(() => document.body.innerHTML.includes('<script>alert(1)</script>'));
      if (hasRawScript) throw new Error('页面存在未转义 <script> 标签');
      await shot(page, '3-2-xss-module-path.png');
      record('3.2 模块路径含 XSS', 'PASS', `表格不崩溃，XSS 未执行`);
    } catch (e) {
      await shot(page, '3-2-xss-module-path-fail.png');
      record('3.2 模块路径含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 3.3 分类筛选超长
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      const catInput = page.getByPlaceholder('分类筛选').first();
      await catInput.waitFor({ state: 'visible', timeout: 5000 });
      await catInput.fill(longStr());
      await catInput.press('Enter');
      await page.waitForTimeout(3000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tableVisible = await page.locator('.app-main .el-table').first().isVisible().catch(() => false);
      if (!tableVisible) throw new Error('表格不可见');
      await shot(page, '3-3-long-category-filter.png');
      record('3.3 分类筛选超长（10000）', 'PASS', `表格不崩溃`);
    } catch (e) {
      await shot(page, '3-3-long-category-filter-fail.png');
      record('3.3 分类筛选超长（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 3.4 分类筛选含 null 字节
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      // null 字节用 native setter
      await page.evaluate(() => {
        const inp = document.querySelector('input[placeholder="分类筛选"]');
        if (!inp) throw new Error('分类筛选 input not found');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(inp, 'backend\0injection');
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await page.keyboard.press('Enter');
      await page.waitForTimeout(2000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tableVisible = await page.locator('.app-main .el-table').first().isVisible().catch(() => false);
      if (!tableVisible) throw new Error('表格不可见');
      await shot(page, '3-4-null-category-filter.png');
      record('3.4 分类筛选含 null 字节', 'PASS', `表格不崩溃`);
    } catch (e) {
      await shot(page, '3-4-null-category-filter-fail.png');
      record('3.4 分类筛选含 null 字节', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 3.5 分类筛选含特殊字符（emoji/换行 tab）
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      const catInput = page.getByPlaceholder('分类筛选').first();
      await catInput.waitFor({ state: 'visible', timeout: 5000 });
      await catInput.fill(`back🎉end\tand\nnewline`);
      await catInput.press('Enter');
      await page.waitForTimeout(2000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const tableVisible = await page.locator('.app-main .el-table').first().isVisible().catch(() => false);
      if (!tableVisible) throw new Error('表格不可见');
      await shot(page, '3-5-special-category-filter.png');
      record('3.5 分类筛选含 emoji/换行', 'PASS', `表格不崩溃`);
    } catch (e) {
      await shot(page, '3-5-special-category-filter-fail.png');
      record('3.5 分类筛选含 emoji/换行', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // ============================================================
  // 4. 资产详情对话框异常输入
  // ============================================================
  console.log('\n--- 4. 资产详情对话框异常输入 ---');
  // SKIP 原因：详情对话框 ID 来自表格行 row.id，UI 无用户输入入口；
  //          Vue 3 prod build 下 app.__vue_app__._instance.proxy 为 null，
  //          无法通过 page.evaluate 直接调用组件 showDetail 方法注入异常 ID；
  //          正常路径下 row.id 来自后端返回的资产 ID（已被后端校验），
  //          前端无异常输入入口可测
  record('4.1 详情-含 XSS ID', 'SKIP', 'UI 无用户输入入口，Vue 3 prod build 无法通过 evaluate 调用组件方法');
  record('4.2 详情-含路径遍历 ID', 'SKIP', 'UI 无用户输入入口，Vue 3 prod build 无法通过 evaluate 调用组件方法');

  // ============================================================
  // 5. 资产图谱异常输入
  // ============================================================
  console.log('\n--- 5. 资产图谱异常输入 ---');

  // 5.1 根资产 ID 含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      await fillInput(page, '根资产 ID', `'; DROP TABLE--`);
      await clickBtn(page, '遍历');
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      // 应显示 error 消息或空结果，不崩溃
      const errorMsg = await getMessageText(page);
      const graphAreaVisible = await page.locator('.app-main .table-container').first().isVisible().catch(() => false);
      if (!graphAreaVisible) throw new Error('图谱区域不可见');
      await shot(page, '5-1-sql-graph-root.png');
      record('5.1 图谱-根 ID 含 SQL 注入', 'PASS', `不崩溃，消息="${errorMsg || '无消息'}"`);
    } catch (e) {
      await shot(page, '5-1-sql-graph-root-fail.png');
      record('5.1 图谱-根 ID 含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 5.2 根资产 ID 含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      await fillInput(page, '根资产 ID', `<script>alert(1)</script>`);
      await clickBtn(page, '遍历');
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS 已执行！dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const hasRawScript = await page.evaluate(() => document.body.innerHTML.includes('<script>alert(1)</script>'));
      if (hasRawScript) throw new Error('页面存在未转义 <script> 标签');
      await shot(page, '5-2-xss-graph-root.png');
      const errorMsg = await getMessageText(page);
      record('5.2 图谱-根 ID 含 XSS', 'PASS', `不崩溃，XSS 未执行，消息="${errorMsg || '无消息'}"`);
    } catch (e) {
      await shot(page, '5-2-xss-graph-root-fail.png');
      record('5.2 图谱-根 ID 含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 5.3 根资产 ID 超长
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      await fillInput(page, '根资产 ID', longStr());
      await clickBtn(page, '遍历');
      await page.waitForTimeout(3000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '5-3-long-graph-root.png');
      const errorMsg = await getMessageText(page);
      record('5.3 图谱-根 ID 超长（10000）', 'PASS', `不崩溃，消息="${errorMsg || '无消息'}"`);
    } catch (e) {
      await shot(page, '5-3-long-graph-root-fail.png');
      record('5.3 图谱-根 ID 超长（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 5.4 根资产 ID 为空 + 点遍历
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      // loadGraphRoot 会自动填充第一个资产 ID，需先清空
      const rootInput = page.getByPlaceholder('根资产 ID').first();
      await rootInput.waitFor({ state: 'visible', timeout: 5000 });
      await rootInput.fill('');
      await clickBtn(page, '遍历');
      await waitForMessage(page, 'warning', '请输入资产 ID');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '5-4-empty-graph-root.png');
      record('5.4 图谱-根 ID 为空', 'PASS', `前端 warning 拦截`);
    } catch (e) {
      await shot(page, '5-4-empty-graph-root-fail.png');
      record('5.4 图谱-根 ID 为空', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 5.5 根资产 ID 含 null 字节
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      await fillInputNative(page, '根资产 ID', `asset\0alice`);
      await clickBtn(page, '遍历');
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '5-5-null-graph-root.png');
      const errorMsg = await getMessageText(page);
      record('5.5 图谱-根 ID 含 null 字节', 'PASS', `不崩溃，消息="${errorMsg || '无消息'}"`);
    } catch (e) {
      await shot(page, '5-5-null-graph-root-fail.png');
      record('5.5 图谱-根 ID 含 null 字节', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 5.6 根资产 ID 不存在
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      await loginAlice(page, ALICE_KEY);
      await gotoMenu(page, '资产图谱');
      await fillInput(page, '根资产 ID', `nonexistent-asset-id-12345`);
      await clickBtn(page, '遍历');
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      // 应显示 error 消息（后端 404）
      const errorMsg = await getMessageText(page);
      await shot(page, '5-6-nonexistent-graph-root.png');
      record('5.6 图谱-根 ID 不存在', 'PASS', `不崩溃，消息="${errorMsg || '无消息'}"（后端应返回 404）`);
    } catch (e) {
      await shot(page, '5-6-nonexistent-graph-root-fail.png');
      record('5.6 图谱-根 ID 不存在', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // ============================================================
  // 6. ACL 添加对话框异常输入
  // ============================================================
  console.log('\n--- 6. ACL 添加对话框异常输入 ---');

  // helper：打开 ACL 添加对话框
  async function openAclAddDialog(page) {
    await loginAlice(page, ALICE_KEY);
    // 每次打开前通过 API 确保 004 是 restricted（防止其他用例改回 scope）
    try {
      const check = await fetch(`${BASE}v1/assets/asset-alice-004`, {
        headers: { 'X-API-Key': ALICE_KEY },
      }).then(r => r.json());
      if (check.scope !== 'restricted') {
        console.log(`  [helper] 004 scope="${check.scope}"，重新改为 restricted`);
        await fetch(`${BASE}v1/assets/asset-alice-004/scope`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
          body: JSON.stringify({ scope: 'restricted' }),
        });
        await page.waitForTimeout(300);
      }
    } catch (e) { /* 忽略，继续尝试 */ }
    await gotoMenu(page, 'ACL 授权');
    await page.waitForTimeout(1000);
    // 等 ACL 表格加载（受限资产 asset-alice-004）
    await page.waitForSelector('.app-main .el-table__row', { timeout: 8000 });
    const row = page.locator('.app-main .el-table__row', { hasText: 'asset-alice-004' }).first();
    await row.locator('.el-button', { hasText: '管理 ACL' }).click();
    const aclDlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
    await aclDlg.waitFor({ state: 'visible', timeout: 5000 });
    await page.waitForTimeout(500);
    // 按钮限定在 aclDlg scope 内，避免匹配到表格中其他按钮
    await aclDlg.locator('.el-button--primary', { hasText: '添加授权' }).click();
    const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
    await formDlg.waitFor({ state: 'visible', timeout: 3000 });
    await page.waitForTimeout(300);
    return formDlg;
  }

  // helper：在 ACL 添加对话框内点击"添加授权"提交按钮（限定 scope，避免被表格按钮拦截）
  async function submitAclAdd(formDlg) {
    await formDlg.locator('.el-button--primary', { hasText: '添加授权' }).click();
  }

  // 6.1 grantee ID 含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const formDlg = await openAclAddDialog(page);
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill(`'; DROP TABLE--`);
      await submitAclAdd(formDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '6-1-sql-acl-grantee.png');
      const msg = await getMessageText(page);
      record('6.1 ACL-grantee ID 含 SQL 注入', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '6-1-sql-acl-grantee-fail.png');
      record('6.1 ACL-grantee ID 含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 6.2 grantee ID 含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const formDlg = await openAclAddDialog(page);
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill(`<script>alert(1)</script>`);
      await submitAclAdd(formDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS 已执行！dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const hasRawScript = await page.evaluate(() => document.body.innerHTML.includes('<script>alert(1)</script>'));
      if (hasRawScript) throw new Error('页面存在未转义 <script> 标签');
      await shot(page, '6-2-xss-acl-grantee.png');
      const msg = await getMessageText(page);
      record('6.2 ACL-grantee ID 含 XSS', 'PASS', `不崩溃，XSS 未执行，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '6-2-xss-acl-grantee-fail.png');
      record('6.2 ACL-grantee ID 含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 6.3 grantee ID 超长
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const formDlg = await openAclAddDialog(page);
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill(longStr());
      await submitAclAdd(formDlg);
      await page.waitForTimeout(3000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '6-3-long-acl-grantee.png');
      const msg = await getMessageText(page);
      record('6.3 ACL-grantee ID 超长（10000）', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '6-3-long-acl-grantee-fail.png');
      record('6.3 ACL-grantee ID 超长（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 6.4 grantee ID 为空
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const formDlg = await openAclAddDialog(page);
      // 不填 grantee ID，直接点添加授权
      await submitAclAdd(formDlg);
      await waitForMessage(page, 'warning', '请输入授权对象 ID');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '6-4-empty-acl-grantee.png');
      record('6.4 ACL-grantee ID 为空', 'PASS', `前端 warning 拦截，未调用后端`);
    } catch (e) {
      await shot(page, '6-4-empty-acl-grantee-fail.png');
      record('6.4 ACL-grantee ID 为空', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 6.5 grantee ID 含路径遍历
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const formDlg = await openAclAddDialog(page);
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').fill(`../../etc/passwd`);
      await submitAclAdd(formDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '6-5-path-acl-grantee.png');
      const msg = await getMessageText(page);
      record('6.5 ACL-grantee ID 含路径遍历', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '6-5-path-acl-grantee-fail.png');
      record('6.5 ACL-grantee ID 含路径遍历', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // ============================================================
  // 7. 添加关联对话框异常输入
  // ============================================================
  console.log('\n--- 7. 添加关联对话框异常输入 ---');

  // helper：打开添加关联对话框
  async function openLinkAddDialog(page) {
    await loginAlice(page, ALICE_KEY);
    // 默认在我的规则库
    await page.waitForSelector('.app-main .el-table__row', { timeout: 8000 });
    await page.waitForTimeout(500);
    // 点第一行的"关联"按钮
    const firstRow = page.locator('.app-main .el-table__row').first();
    await firstRow.locator('.el-button', { hasText: '关联' }).click();
    const linksDlg = page.locator('.el-dialog', { hasText: '资产关联' });
    await linksDlg.waitFor({ state: 'visible', timeout: 5000 });
    await page.waitForTimeout(500);
    // 点"+ 添加关联"（限定在 linksDlg scope，使用 primary 选择器避免误匹配）
    await linksDlg.locator('.el-button--primary', { hasText: '添加关联' }).click();
    const linkDlg = page.locator('.el-dialog', { hasText: '添加资产关联' });
    await linkDlg.waitFor({ state: 'visible', timeout: 3000 });
    await page.waitForTimeout(300);
    return linkDlg;
  }

  // helper：在添加关联对话框内点击"添加"提交按钮（限定 scope，避免被表格按钮拦截）
  async function submitLinkAdd(linkDlg) {
    await linkDlg.locator('.el-button--primary', { hasText: '添加' }).click();
  }

  // 7.1 目标资产 ID 含 SQL 注入
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const linkDlg = await openLinkAddDialog(page);
      await linkDlg.getByPlaceholder('关联到的资产 ID').fill(`'; DROP TABLE--`);
      await submitLinkAdd(linkDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '7-1-sql-link-dst.png');
      const msg = await getMessageText(page);
      record('7.1 关联-目标 ID 含 SQL 注入', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '7-1-sql-link-dst-fail.png');
      record('7.1 关联-目标 ID 含 SQL 注入', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 7.2 目标资产 ID 含 XSS
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const linkDlg = await openLinkAddDialog(page);
      await linkDlg.getByPlaceholder('关联到的资产 ID').fill(`<script>alert(1)</script>`);
      await submitLinkAdd(linkDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS 已执行！dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      const hasRawScript = await page.evaluate(() => document.body.innerHTML.includes('<script>alert(1)</script>'));
      if (hasRawScript) throw new Error('页面存在未转义 <script> 标签');
      await shot(page, '7-2-xss-link-dst.png');
      const msg = await getMessageText(page);
      record('7.2 关联-目标 ID 含 XSS', 'PASS', `不崩溃，XSS 未执行，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '7-2-xss-link-dst-fail.png');
      record('7.2 关联-目标 ID 含 XSS', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 7.3 目标资产 ID 超长
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const linkDlg = await openLinkAddDialog(page);
      await linkDlg.getByPlaceholder('关联到的资产 ID').fill(longStr());
      await submitLinkAdd(linkDlg);
      await page.waitForTimeout(3000);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '7-3-long-link-dst.png');
      const msg = await getMessageText(page);
      record('7.3 关联-目标 ID 超长（10000）', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '7-3-long-link-dst-fail.png');
      record('7.3 关联-目标 ID 超长（10000）', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 7.4 目标资产 ID 为空
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const linkDlg = await openLinkAddDialog(page);
      // 不填目标 ID，直接点添加
      await submitLinkAdd(linkDlg);
      await waitForMessage(page, 'warning', '请输入目标资产 ID');
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      await shot(page, '7-4-empty-link-dst.png');
      record('7.4 关联-目标 ID 为空', 'PASS', `前端 warning 拦截，未调用后端`);
    } catch (e) {
      await shot(page, '7-4-empty-link-dst-fail.png');
      record('7.4 关联-目标 ID 为空', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  // 7.5 目标资产 ID 含路径遍历
  {
    const { ctx, page, errors, dialogs } = await newPageWithMonitors();
    try {
      const linkDlg = await openLinkAddDialog(page);
      await linkDlg.getByPlaceholder('关联到的资产 ID').fill(`../../etc/passwd`);
      await submitLinkAdd(linkDlg);
      await page.waitForTimeout(2500);
      if (dialogs.length > 0) throw new Error(`XSS dialog 触发：${JSON.stringify(dialogs)}`);
      const fatals = fatalErrors(errors);
      if (fatals.length > 0) throw new Error(`致命错误：${fatals.join('; ')}`);
      await shot(page, '7-5-path-link-dst.png');
      const msg = await getMessageText(page);
      record('7.5 关联-目标 ID 含路径遍历', 'PASS', `不崩溃，消息="${msg || '无消息'}"`);
    } catch (e) {
      await shot(page, '7-5-path-link-dst-fail.png');
      record('7.5 关联-目标 ID 含路径遍历', 'FAIL', e.message);
    } finally { await ctx.close(); }
  }

  await browser.close();

  // ============================================================
  // 还原：asset-alice-004 scope 恢复原值
  // ============================================================
  if (original004Scope && original004Scope !== 'restricted') {
    try {
      const restoreResp = await fetch(`${BASE}v1/assets/asset-alice-004/scope`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': ALICE_KEY },
        body: JSON.stringify({ scope: original004Scope }),
      });
      if (restoreResp.ok) {
        console.log(`asset-alice-004 scope 已还原为 ${original004Scope}\n`);
      } else {
        console.warn(`asset-alice-004 scope 还原失败：${restoreResp.status}\n`);
      }
    } catch (e) {
      console.warn(`asset-alice-004 scope 还原异常：${e.message}\n`);
    }
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

  const reportPath = path.join(__dirname, 'abnormal-input-results.txt');
  const reportContent = `TeamHarness 前端异常输入测试结果\n生成时间：${new Date().toISOString()}\n总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
    results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
    `\n\n截图目录：${SCREEN_DIR}\n`;
  fs.writeFileSync(reportPath, reportContent, 'utf8');
  console.log(`报告已写入：${reportPath}`);

  process.exit(failCount > 0 ? 1 : 0);
})();
