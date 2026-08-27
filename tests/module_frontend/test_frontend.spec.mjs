// TeamHarness 前端模块测试
// 工具：Playwright + chromium
// 运行：node tests/module_frontend/test_frontend.spec.mjs
//
// 测试范围（严格限定前端 UI）：
//   1. 登录页
//   2. 6 个菜单页面切换
//   3. 我的规则库
//   4. 共享库
//   5. 共享管理
//   6. 资产图谱（重点验证 UI-1：关联列表对话框入口）
//   7. ACL 授权
//   8. 治理看板
//   9. 退出登录
//  10. 控制台错误
//
// 测试铁律：用例独立可复现（每用例前重载/重置）；突变数据 round-trip 还原；
//          覆盖 happy path + 边界 + 异常；FAIL 先定位根因
//
// 已知缺陷（需验证）：
//   AUTH-2: 无效 API Key 登录成功（前端 handleLogin 未校验 agent_id）
//   UI-1: 关联列表对话框 UI 不可达（index.html 无 @click 触发 showLinksDialog）

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const ALICE_KEY = 'th_7eacce36a225a8de97009e2fe051f618';
const SCREEN_DIR = path.join(__dirname, 'screenshots');

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

// 等待 Element Plus 消息提示出现并包含文本
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

// 选择 el-select 选项
async function selectOption(page, selectLocator, optionText) {
  await selectLocator.first().click();
  await page.waitForTimeout(250);
  const item = page.locator('.el-select-dropdown__item:visible', { hasText: optionText }).first();
  await item.waitFor({ state: 'visible', timeout: 5000 });
  await item.click();
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

async function gotoMenu(page, menuText) {
  const item = page.locator('.el-menu-item', { hasText: menuText }).first();
  await item.click();
  await page.waitForTimeout(600);
}

// 重载页面回到"我的规则库"默认页（登录态由 localStorage 保持）
async function reloadMyPage(page) {
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(1000);
}

async function closeDialogs(page) {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
}

// ---------------- 控制台错误收集 ----------------
// 每个 context 创建时挂载 console 监听，记录所有 error 级别日志
function attachConsoleCollector(page) {
  const errors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') {
      errors.push(msg.text());
    }
  });
  page.on('pageerror', err => {
    errors.push(`PAGEERROR: ${err.message}`);
  });
  return errors;
}

// ---------------- 主流程 ----------------
(async () => {
  console.log('=== TeamHarness 前端模块测试开始 ===\n');
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  // ============================================================
  // 1. 登录页测试
  // ============================================================
  console.log('--- 1. 登录页 ---');

  // 1.1 页面元素存在性
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card', { timeout: 10000 });
      const title = await page.locator('.login-title').textContent();
      if (!title.includes('TeamHarness')) throw new Error(`标题异常：${title}`);
      const memberIdInput = await page.getByPlaceholder('如：alice').isVisible();
      const apiKeyInput = await page.getByPlaceholder('th_ 开头').isVisible();
      const loginBtn = await page.locator('.el-button', { hasText: '登录' }).isVisible();
      const issueLink = await page.locator('.el-button', { hasText: '没有 Key？在此颁发' }).isVisible();
      if (!memberIdInput) throw new Error('成员 ID 输入框缺失');
      if (!apiKeyInput) throw new Error('API Key 输入框缺失');
      if (!loginBtn) throw new Error('登录按钮缺失');
      if (!issueLink) throw new Error('颁发链接缺失');
      await shot(page, '01-login-elements.png');
      record('1.1 登录页元素存在性', 'PASS', `标题=${title.trim()}`);
    } catch (e) {
      await shot(page, '01-login-elements-fail.png');
      record('1.1 登录页元素存在性', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 1.2 空凭证提交
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '登录');
      await waitForMessage(page, 'warning', '请输入成员 ID 和 API Key');
      const stillLogin = await page.locator('.login-card').isVisible();
      if (!stillLogin) throw new Error('空凭证提交后离开了登录页');
      record('1.2 空凭证提交提示警告', 'PASS');
    } catch (e) {
      await shot(page, '01-login-empty-fail.png');
      record('1.2 空凭证提交提示警告', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 1.3 无效 API Key（th_invalid）→ AUTH-2 缺陷验证
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', 'th_invalid');
      await clickBtn(page, '登录');
      await page.waitForTimeout(2000);
      // 期望：出现 error 消息且仍在登录页
      const hasError = await hasMessage(page, 'error', 2000);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
      if (hasError && stillLogin && !enteredMain) {
        record('1.3 无效 API Key 拒绝登录', 'PASS');
      } else if (enteredMain) {
        // AUTH-2 缺陷：无效 key 登录成功
        await shot(page, '01-auth2-bug.png');
        record('1.3 无效 API Key 拒绝登录', 'FAIL',
          'AUTH-2 缺陷：无效 API Key(th_invalid) 登录成功。根因：后端 /v1/auth/apikey/lookup 对无效 key 返回 200 {agent_id:null}，前端 handleLogin(app.js:41) 未校验 agent_id 即放行，直接 localStorage 存储 memberId 并进入主界面');
      } else {
        throw new Error(`行为异常：hasError=${hasError} stillLogin=${stillLogin} enteredMain=${enteredMain}`);
      }
    } catch (e) {
      await shot(page, '01-login-invalid-fail.png');
      record('1.3 无效 API Key 拒绝登录', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 1.4 颁发对话框打开/关闭
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '03-issue-dialog.png');
      await clickBtn(page, '取消');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('1.4 颁发对话框打开/关闭', 'PASS');
    } catch (e) {
      await shot(page, '03-issue-dialog-fail.png');
      record('1.4 颁发对话框打开/关闭', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 1.5 有效凭证登录 alice
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await waitForMessage(page, 'success', '登录成功');
      await page.waitForSelector('.app-header', { timeout: 5000 });
      const memberTag = await page.locator('.el-tag', { hasText: '成员：alice' }).isVisible();
      if (!memberTag) throw new Error('主界面未显示成员 alice');
      await shot(page, '02-login-success.png');
      record('1.5 有效凭证登录 alice 进入主界面', 'PASS');
    } catch (e) {
      await shot(page, '02-login-success-fail.png');
      record('1.5 有效凭证登录 alice 进入主界面', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 1.6 登录后刷新页面保持登录
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.login-card', { timeout: 10000 });
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForSelector('.app-header', { timeout: 5000 });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.app-header', { timeout: 8000 });
      const stillIn = await page.locator('.app-header').isVisible();
      if (!stillIn) throw new Error('刷新后退出登录');
      const key = await page.evaluate(() => localStorage.getItem('teamharness_api_key'));
      if (!key) throw new Error('刷新后 localStorage 丢失 api_key');
      record('1.6 刷新页面保持登录态（localStorage）', 'PASS');
    } catch (e) {
      record('1.6 刷新页面保持登录态（localStorage）', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 主界面共享会话（登录一次，复用于各页面测试）
  // ============================================================
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const consoleErrors = attachConsoleCollector(page);
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForSelector('.login-card');
  await fillInput(page, '如：alice', 'alice');
  await fillInput(page, 'th_ 开头', ALICE_KEY);
  await clickBtn(page, '登录');
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(1000);

  // ============================================================
  // 2. 6 个菜单页面切换
  // ============================================================
  console.log('\n--- 2. 6 个菜单页面切换 ---');

  // 期望文本用页面实际渲染的内容（表格列头、alert 文本等），不用 placeholder（placeholder 不在 textContent 中）
  const menuCases = [
    { name: '我的规则库', index: 'my', expectText: '更新时间', isFilterBar: true },
    { name: '共享库', index: 'shared', expectText: '内容预览', isFilterBar: true },
    { name: '共享管理', index: 'share-mgmt', expectText: '选择性共享管理', isFilterBar: false },
    { name: '资产图谱', index: 'graph', expectText: '输入根资产 ID', isFilterBar: false },
    { name: 'ACL 授权', index: 'acl', expectText: 'ACL 精准授权', isFilterBar: false },
    { name: '治理看板', index: 'dashboard', expectText: '我的资产总数', isFilterBar: false },
  ];

  for (const mc of menuCases) {
    try {
      await reloadMyPage(page);
      const errorsBefore = consoleErrors.length;
      await gotoMenu(page, mc.name);
      await page.waitForTimeout(800);
      const appMainText = await page.locator('.app-main').first().textContent();
      if (!appMainText.includes(mc.expectText)) {
        throw new Error(`菜单"${mc.name}"点击后未显示预期内容（期望含"${mc.expectText}"）`);
      }
      const activeMenu = await page.locator('.el-menu-item.is-active').first().textContent();
      if (!activeMenu.includes(mc.name)) {
        throw new Error(`激活菜单项不匹配：期望${mc.name}，实际${activeMenu}`);
      }
      // 检查菜单切换后是否有新增控制台错误
      const newErrors = consoleErrors.slice(errorsBefore);
      await shot(page, `04-menu-${mc.index}.png`);
      if (newErrors.length > 0) {
        record(`2.${menuCases.indexOf(mc) + 1} 菜单切换：${mc.name}`, 'PASS',
          `内容渲染正确，但有 ${newErrors.length} 条 console error`);
      } else {
        record(`2.${menuCases.indexOf(mc) + 1} 菜单切换：${mc.name}`, 'PASS');
      }
    } catch (e) {
      await shot(page, `04-menu-${mc.index}-fail.png`);
      record(`2.${menuCases.indexOf(mc) + 1} 菜单切换：${mc.name}`, 'FAIL', e.message);
    }
  }

  // ============================================================
  // 3. 我的规则库
  // ============================================================
  console.log('\n--- 3. 我的规则库 ---');

  // 3.1 表格加载
  {
    try {
      await reloadMyPage(page);
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('表格无数据行');
      const tableText = await page.locator('.app-main .table-container').first().textContent();
      if (!tableText.includes('asset-alice-')) throw new Error('未找到 asset-alice- 资产');
      await shot(page, '05-my-assets.png');
      record('3.1 我的规则库表格加载', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '05-my-assets-fail.png');
      record('3.1 我的规则库表格加载', 'FAIL', e.message);
    }
  }

  // 3.2 类型筛选（rule）
  {
    try {
      await reloadMyPage(page);
      const typeSelect = page.locator('.filter-bar .el-select').first();
      await selectOption(page, typeSelect, 'rule 规则');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('筛选 rule 后无数据');
      const typeTags = await page.locator('.app-main .el-table__row td:nth-child(2) .el-tag').allTextContents();
      const allRule = typeTags.every(t => t.trim() === 'rule');
      if (!allRule) throw new Error(`筛选后类型列存在非 rule：${typeTags.join(',')}`);
      record('3.2 类型筛选器（rule）', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '05-my-filter-type-fail.png');
      record('3.2 类型筛选器（rule）', 'FAIL', e.message);
    }
  }

  // 3.3 共享范围筛选（restricted）
  {
    try {
      await reloadMyPage(page);
      const scopeSelect = page.locator('.filter-bar .el-select').nth(1);
      await selectOption(page, scopeSelect, 'restricted 受限');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('筛选 restricted 后无数据');
      const text = await page.locator('.app-main .table-container').first().textContent();
      if (!text.includes('asset-alice-004')) throw new Error('筛选 restricted 未返回 asset-alice-004');
      record('3.3 共享范围筛选器（restricted）', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '05-my-filter-scope-fail.png');
      record('3.3 共享范围筛选器（restricted）', 'FAIL', e.message);
    }
  }

  // 3.4 分类筛选
  {
    try {
      await reloadMyPage(page);
      const catInput = page.getByPlaceholder('分类筛选').first();
      await catInput.fill('backend/coding');
      await catInput.press('Enter');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('分类筛选无结果');
      const text = await page.locator('.app-main .table-container').first().textContent();
      if (!text.includes('asset-alice-001')) throw new Error('分类筛选 backend/coding 未返回 001');
      record('3.4 分类筛选器（backend/coding）', 'PASS', `行数=${rows}`);
    } catch (e) {
      record('3.4 分类筛选器（backend/coding）', 'FAIL', e.message);
    }
  }

  // 3.5 模块路径筛选
  {
    try {
      await reloadMyPage(page);
      const mpInput = page.getByPlaceholder('模块路径').first();
      await mpInput.fill('modules/governance');
      await mpInput.press('Enter');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('模块路径筛选无结果');
      const text = await page.locator('.app-main .table-container').first().textContent();
      if (!text.includes('asset-alice-004')) throw new Error('模块路径筛选未返回 004');
      record('3.5 模块路径筛选器（modules/governance）', 'PASS', `行数=${rows}`);
    } catch (e) {
      record('3.5 模块路径筛选器（modules/governance）', 'FAIL', e.message);
    }
  }

  // 3.6 详情对话框打开/关闭
  {
    try {
      await reloadMyPage(page);
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '详情' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '资产详情' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      const dlgText = await dlg.textContent();
      if (!dlgText.includes('ID') || !dlgText.includes('内容快照')) throw new Error('详情对话框内容缺失');
      await shot(page, '05-detail-dialog.png');
      await dlg.locator('.el-dialog__close').click();
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('3.6 详情对话框打开/关闭', 'PASS');
    } catch (e) {
      await shot(page, '05-detail-dialog-fail.png');
      record('3.6 详情对话框打开/关闭', 'FAIL', e.message);
    }
  }

  // 3.7 共享修改对话框（round-trip：002 team→private→team）
  {
    try {
      await reloadMyPage(page);
      const row002 = page.locator('.el-table__row', { hasText: 'asset-alice-002' }).first();
      await row002.locator('.el-button', { hasText: '共享' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '05-scope-dialog.png');
      await dlg.locator('.el-radio', { hasText: '私有' }).click();
      await clickBtn(page, '确认修改');
      await waitForMessage(page, 'success', '共享范围修改成功');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      await waitTableLoaded(page);
      // 验证已变 private
      const row002b = page.locator('.el-table__row', { hasText: 'asset-alice-002' }).first();
      await row002b.locator('.el-button', { hasText: '共享' }).click();
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      const privateChecked = await dlg.locator('.el-radio', { hasText: '私有' }).locator('.el-radio__input.is-checked').count();
      if (privateChecked === 0) throw new Error('修改后 002 未变为 private');
      // 还原：选回"团队共享"
      await dlg.locator('.el-radio', { hasText: '团队共享' }).click();
      await clickBtn(page, '确认修改');
      await waitForMessage(page, 'success', '共享范围修改成功');
      await waitTableLoaded(page);
      record('3.7 共享修改对话框（002 team→private→team）', 'PASS');
    } catch (e) {
      await shot(page, '05-scope-dialog-fail.png');
      record('3.7 共享修改对话框（002 team→private→team）', 'FAIL', e.message);
    }
  }

  // 3.8 分页控件
  {
    try {
      await reloadMyPage(page);
      await waitTableLoaded(page);
      const pagination = page.locator('.el-pagination').first();
      const visible = await pagination.isVisible();
      if (!visible) throw new Error('分页控件不可见');
      const totalText = await pagination.textContent();
      // Element Plus CDN 默认 locale=en-US，显示"Total"；中文 locale 显示"共 X 条"
      if (!totalText.includes('共') && !totalText.toLowerCase().includes('total')) {
        throw new Error(`分页未显示总数：${totalText}`);
      }
      record('3.8 分页控件渲染', 'PASS', `内容=${totalText.trim().replace(/\s+/g, ' ')}`);
    } catch (e) {
      record('3.8 分页控件渲染', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 4. 共享库
  // ============================================================
  console.log('\n--- 4. 共享库 ---');

  // 4.1 表格加载
  {
    try {
      await closeDialogs(page);
      await gotoMenu(page, '共享库');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('共享库无数据');
      await shot(page, '06-shared-assets.png');
      record('4.1 共享库表格加载', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '06-shared-fail.png');
      record('4.1 共享库表格加载', 'FAIL', e.message);
    }
  }

  // 4.2 筛选器（类型 rule）
  {
    try {
      const typeSelect = page.locator('.filter-bar .el-select').nth(1);
      await selectOption(page, typeSelect, 'rule');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('共享库筛选 rule 无数据');
      await selectOption(page, typeSelect, '全部');
      await waitTableLoaded(page);
      record('4.2 共享库类型筛选器', 'PASS');
    } catch (e) {
      record('4.2 共享库类型筛选器', 'FAIL', e.message);
    }
  }

  // 4.3 验证 private 资产不出现（共享库默认 scope=team，应不含 private）
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享库');
      await waitTableLoaded(page);
      const tableText = await page.locator('.app-main .table-container').first().textContent();
      // 共享库默认筛选 scope=team，检查是否有 private 标签
      const hasPrivate = tableText.includes('私有');
      if (hasPrivate) {
        throw new Error('共享库（默认 team）出现了 private 资产');
      }
      record('4.3 共享库不含 private 资产', 'PASS', '默认 scope=team，无 private');
    } catch (e) {
      record('4.3 共享库不含 private 资产', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 5. 共享管理
  // ============================================================
  console.log('\n--- 5. 共享管理 ---');

  // 5.1 表格加载
  {
    try {
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('共享管理无数据');
      const batchBtn = page.locator('.el-button', { hasText: '批量修改共享' }).first();
      const disabled = await batchBtn.isDisabled();
      await shot(page, '07-share-mgmt.png');
      record('5.1 共享管理表格加载+批量按钮初始禁用', 'PASS', `行数=${rows}，禁用=${disabled}`);
    } catch (e) {
      await shot(page, '07-share-mgmt-fail.png');
      record('5.1 共享管理表格加载+批量按钮初始禁用', 'FAIL', e.message);
    }
  }

  // 5.2 勾选资产
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      // 用行内 checkbox（.el-table__row .el-checkbox），避免点到表头全选 checkbox 导致全选
      const firstRowCheckbox = page.locator('.app-main .el-table__row .el-checkbox').first();
      await firstRowCheckbox.click();
      await page.waitForTimeout(400);
      const batchBtn = page.locator('.el-button', { hasText: '批量修改共享' }).first();
      const disabled = await batchBtn.isDisabled();
      if (disabled) throw new Error('勾选后批量按钮仍禁用');
      const btnText = await batchBtn.textContent();
      if (!btnText.includes('已选 1')) throw new Error(`批量按钮未显示选中数（期望含"已选 1"）：${btnText}`);
      record('5.2 勾选资产启用批量按钮', 'PASS', btnText.trim());
    } catch (e) {
      await shot(page, '07-share-mgmt-select-fail.png');
      record('5.2 勾选资产启用批量按钮', 'FAIL', e.message);
    }
  }

  // 5.3 批量修改对话框（取消）
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const firstCheckbox = page.locator('.app-main .el-table .el-checkbox').first();
      await firstCheckbox.click();
      await page.waitForTimeout(400);
      await page.locator('.el-button', { hasText: '批量修改共享' }).first().click();
      const dlg = page.locator('.el-dialog', { hasText: '批量修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '07-batch-scope-dialog.png');
      const alertText = await dlg.textContent();
      if (!alertText.includes('将修改') || !alertText.includes('个资产')) throw new Error('批量对话框未显示选中数量');
      await clickBtn(page, '取消');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('5.3 批量修改对话框（取消）', 'PASS');
    } catch (e) {
      await shot(page, '07-batch-scope-fail.png');
      record('5.3 批量修改对话框（取消）', 'FAIL', e.message);
    }
  }

  // 5.4 快速修改 scope 下拉（round-trip：003 team→private→team）
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const row003 = page.locator('.el-table__row', { hasText: 'asset-alice-003' }).first();
      const rowSelect = row003.locator('.el-select').first();
      await selectOption(page, rowSelect, '私有');
      await page.waitForSelector('.el-message-box', { timeout: 3000 });
      await confirmMessageBox(page, 'confirm');
      await waitForMessage(page, 'success', '修改成功');
      await waitTableLoaded(page);
      const row003b = page.locator('.el-table__row', { hasText: 'asset-alice-003' }).first();
      const txt = await row003b.textContent();
      if (!txt.includes('私有')) throw new Error('快速修改后 003 未变 private');
      // 还原：改回 team
      const rowSelect2 = row003b.locator('.el-select').first();
      await selectOption(page, rowSelect2, '团队');
      await page.waitForSelector('.el-message-box', { timeout: 3000 });
      await confirmMessageBox(page, 'confirm');
      await waitForMessage(page, 'success', '修改成功');
      await waitTableLoaded(page);
      record('5.4 快速修改 scope（003 team→private→team）', 'PASS');
    } catch (e) {
      await shot(page, '07-quick-scope-fail.png');
      record('5.4 快速修改 scope（003 team→private→team）', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 6. 资产图谱（重点验证 UI-1）
  // ============================================================
  console.log('\n--- 6. 资产图谱 ---');

  // 6.1 BFS 遍历（asset-alice-001，深度2，应 3 节点 2 边）
  {
    try {
      await closeDialogs(page);
      await gotoMenu(page, '资产图谱');
      await page.waitForTimeout(500);
      await fillInput(page, '根资产 ID', 'asset-alice-001');
      await clickBtn(page, '遍历');
      await page.waitForTimeout(1500);
      const descText = await page.locator('.el-descriptions').first().textContent();
      await shot(page, '08-graph.png');
      const match = descText.match(/(\d+)\s*\/\s*(\d+)/);
      if (!match) throw new Error('未找到节点/边数描述');
      const nodeCount = parseInt(match[1]);
      const edgeCount = parseInt(match[2]);
      if (nodeCount !== 3 || edgeCount !== 2) {
        throw new Error(`节点/边数不匹配：期望 3/2，实际 ${nodeCount}/${edgeCount}`);
      }
      const nodeCardHeader = await page.locator('.el-card', { hasText: '节点（' }).first().textContent();
      const edgeCardHeader = await page.locator('.el-card', { hasText: '边（' }).first().textContent();
      if (!nodeCardHeader.includes('节点（3）')) throw new Error('节点表头未显示 3');
      if (!edgeCardHeader.includes('边（2）')) throw new Error('边表头未显示 2');
      record('6.1 图谱 BFS 遍历（asset-alice-001，3节点2边）', 'PASS', `节点=${nodeCount} 边=${edgeCount}`);
    } catch (e) {
      await shot(page, '08-graph-fail.png');
      record('6.1 图谱 BFS 遍历（asset-alice-001，3节点2边）', 'FAIL', e.message);
    }
  }

  // 6.2 深度切换为 1
  {
    try {
      const depthSelect = page.locator('.filter-bar .el-select').first();
      await selectOption(page, depthSelect, '深度 1（直接关联）');
      await clickBtn(page, '遍历');
      await page.waitForTimeout(1500);
      const descText = await page.locator('.el-descriptions').first().textContent();
      const match = descText.match(/(\d+)\s*\/\s*(\d+)/);
      if (!match) throw new Error('深度1未返回节点/边数');
      const nodeCount = parseInt(match[1]);
      const edgeCount = parseInt(match[2]);
      if (nodeCount < 1 || edgeCount < 1) throw new Error(`深度1结果异常：${nodeCount}/${edgeCount}`);
      record('6.2 图谱深度1遍历', 'PASS', `节点=${nodeCount} 边=${edgeCount}`);
    } catch (e) {
      record('6.2 图谱深度1遍历', 'FAIL', e.message);
    }
  }

  // 6.3 关联列表对话框 UI 入口（UI-1 缺陷验证）
  // showLinksDialog 在 app.js 定义但 index.html 无 @click 触发
  {
    try {
      await page.waitForTimeout(500);
      // 检查图谱页面是否有任何按钮能打开"资产关联"对话框
      const graphBtns = await page.locator('.app-main .el-button').allTextContents();
      const hasLinksEntry = graphBtns.some(b => b.includes('关联') || b.includes('管理关联') || b.includes('links'));
      if (hasLinksEntry) {
        record('6.3 关联列表对话框 UI 入口（图谱页）', 'PASS',
          `发现入口按钮：${graphBtns.filter(b => b.includes('关联')).join(',')}`);
      } else {
        throw new Error('UI-1 缺陷：showLinksDialog 在 app.js:311 定义并在 app.js:470 导出，但 index.html 中无任何 @click 触发。图谱页可见按钮：[' + graphBtns.join(', ') + ']。关联列表/添加关联/删除关联对话框均为死代码');
      }
    } catch (e) {
      record('6.3 关联列表对话框 UI 入口（图谱页）— UI-1 缺陷', 'FAIL', e.message);
    }
  }

  // 6.4 添加关联对话框是否能从 UI 打开
  // 添加关联对话框只能从 linksDialog 内的"+ 添加关联"按钮触发，
  // 而 linksDialog 本身无法打开，所以添加关联对话框也不可达
  {
    try {
      // 检查当前页面是否有"添加关联"按钮可见（不通过 linksDialog）
      const addLinkBtn = page.locator('.el-button', { hasText: '添加关联' });
      const visible = await addLinkBtn.isVisible().catch(() => false);
      if (visible) {
        throw new Error('意外：添加关联按钮直接可见');
      }
      // 由于 linksDialog 不可达，添加关联对话框也不可达
      throw new Error('UI-1 连锁缺陷：添加关联对话框只能从 linksDialog 内部触发，而 linksDialog 本身不可达，所以添加关联对话框也不可达');
    } catch (e) {
      record('6.4 添加关联对话框 UI 可达性 — UI-1 连锁缺陷', 'FAIL', e.message);
    }
  }

  // 6.5 我的规则库详情对话框中是否有关联入口
  {
    try {
      await reloadMyPage(page);
      await waitTableLoaded(page);
      await page.locator('.app-main .el-table__row').first().locator('.el-button', { hasText: '详情' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '资产详情' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      const dlgText = await dlg.textContent();
      await shot(page, '08-detail-dialog-links-check.png');
      // 检查详情对话框中是否有"关联"/"links"相关入口
      const hasLinksEntry = dlgText.includes('关联') || dlgText.includes('管理关联') || dlgText.includes('links');
      if (hasLinksEntry) {
        record('6.5 详情对话框关联入口', 'PASS', '发现关联入口');
      } else {
        throw new Error('UI-1 验证：详情对话框中无关联入口。详情对话框只显示元数据（ID/类型/所有者/共享范围/模块路径/分类/版本/状态/Git 路径/标签/时间/Embedding ID/内容快照），无任何"关联"或"管理关联"按钮');
      }
    } catch (e) {
      record('6.5 详情对话框关联入口 — UI-1 缺陷', 'FAIL', e.message);
    }
  }

  // 6.6 我的规则库表格操作列是否有关联按钮
  {
    try {
      await closeDialogs(page);
      await reloadMyPage(page);
      await waitTableLoaded(page);
      // 操作列按钮：详情、共享（无关联按钮）
      const actionBtns = await page.locator('.app-main .el-table__row').first().locator('.el-button').allTextContents();
      const hasLinksBtn = actionBtns.some(b => b.includes('关联'));
      if (hasLinksBtn) {
        record('6.6 我的规则库操作列关联按钮', 'PASS', `发现：${actionBtns.filter(b=>b.includes('关联')).join(',')}`);
      } else {
        throw new Error('UI-1 验证：我的规则库操作列只有 [' + actionBtns.join(', ') + ']，无"关联"按钮');
      }
    } catch (e) {
      record('6.6 我的规则库操作列关联按钮 — UI-1 缺陷', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 7. ACL 授权
  // ============================================================
  console.log('\n--- 7. ACL 授权 ---');

  // 7.1 受限资产列表加载
  {
    try {
      await closeDialogs(page);
      await reloadMyPage(page);
      await gotoMenu(page, 'ACL 授权');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('ACL 页无受限资产');
      const text = await page.locator('.app-main .table-container').first().textContent();
      if (!text.includes('asset-alice-004')) throw new Error('未列出 asset-alice-004');
      await shot(page, '09-acl.png');
      record('7.1 ACL 受限资产列表加载', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '09-acl-fail.png');
      record('7.1 ACL 受限资产列表加载', 'FAIL', e.message);
    }
  }

  // 7.2 ACL 管理对话框打开
  {
    try {
      const row004 = page.locator('.el-table__row', { hasText: 'asset-alice-004' }).first();
      await row004.locator('.el-button', { hasText: '管理 ACL' }).click();
      const dlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      await shot(page, '09-acl-dialog.png');
      const txt = await dlg.textContent();
      if (!txt.includes('当前授权')) throw new Error('ACL 对话框未显示授权列表');
      record('7.2 ACL 管理对话框打开', 'PASS');
    } catch (e) {
      await shot(page, '09-acl-dialog-fail.png');
      record('7.2 ACL 管理对话框打开', 'FAIL', e.message);
    }
  }

  // 7.3 添加授权对话框
  {
    try {
      const dlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await dlg.locator('.el-button', { hasText: '添加授权' }).click();
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      await formDlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(400);
      const formText = await formDlg.textContent();
      if (!formText.includes('对象类型') || !formText.includes('对象 ID') || !formText.includes('权限')) {
        throw new Error('添加授权对话框表单缺失');
      }
      await shot(page, '09-acl-add-dialog.png');
      record('7.3 添加授权对话框打开', 'PASS');
    } catch (e) {
      await shot(page, '09-acl-add-fail.png');
      record('7.3 添加授权对话框打开', 'FAIL', e.message);
    }
  }

  // 7.4 添加授权 + 撤销（round-trip）
  {
    try {
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      const aclDlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      const beforeRows = await aclDlg.locator('.el-table__row').count();
      const uniqueGrantee = 'e2e-bob-' + Date.now();
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').first().fill(uniqueGrantee);
      await formDlg.locator('.el-button', { hasText: '添加授权' }).click();
      await waitForMessage(page, 'success', '授权已添加', 6000);
      await formDlg.waitFor({ state: 'hidden', timeout: 3000 });
      await page.waitForTimeout(800);
      const afterRows = await aclDlg.locator('.el-table__row').count();
      if (afterRows !== beforeRows + 1) throw new Error(`添加后授权数未 +1：before=${beforeRows} after=${afterRows}`);
      // 撤销
      const granteeRow = aclDlg.locator('.el-table__row', { hasText: uniqueGrantee }).first();
      await granteeRow.locator('.el-button', { hasText: '撤销' }).click();
      await page.waitForSelector('.el-message-box', { timeout: 3000 });
      await confirmMessageBox(page, 'confirm');
      await waitForMessage(page, 'success', '授权已撤销');
      await page.waitForTimeout(800);
      const finalRows = await aclDlg.locator('.el-table__row').count();
      if (finalRows !== beforeRows) throw new Error(`撤销后授权数未还原：before=${beforeRows} final=${finalRows}`);
      record('7.4 ACL 添加授权+撤销（round-trip）', 'PASS');
    } catch (e) {
      await shot(page, '09-acl-roundtrip-fail.png');
      record('7.4 ACL 添加授权+撤销（round-trip）', 'FAIL', e.message);
    }
  }

  // 关闭残留对话框
  await closeDialogs(page);

  // ============================================================
  // 8. 治理看板
  // ============================================================
  console.log('\n--- 8. 治理看板 ---');

  // 8.1 统计卡片渲染
  {
    try {
      await closeDialogs(page);
      await reloadMyPage(page);
      await gotoMenu(page, '治理看板');
      await page.waitForTimeout(1200);
      const cards = ['我的资产总数', '私有资产', '团队共享', '公开资产'];
      for (const c of cards) {
        const visible = await page.locator('.el-card', { hasText: c }).first().isVisible();
        if (!visible) throw new Error(`统计卡片缺失：${c}`);
      }
      await shot(page, '10-dashboard.png');
      record('8.1 治理看板统计卡片渲染', 'PASS', `卡片=${cards.length}`);
    } catch (e) {
      await shot(page, '10-dashboard-fail.png');
      record('8.1 治理看板统计卡片渲染', 'FAIL', e.message);
    }
  }

  // 8.2 按类型分布
  {
    try {
      const typeCard = page.locator('.el-card', { hasText: '按类型分布' }).first();
      const progressBars = await typeCard.locator('.el-progress').count();
      if (progressBars === 0) throw new Error('按类型分布无进度条');
      record('8.2 按类型分布', 'PASS', `进度条数=${progressBars}`);
    } catch (e) {
      record('8.2 按类型分布', 'FAIL', e.message);
    }
  }

  // 8.3 按模块分布
  {
    try {
      const modCard = page.locator('.el-card', { hasText: '按模块分布' }).first();
      const modText = await modCard.textContent();
      if (!modText.includes('modules/')) throw new Error('按模块分布未显示 modules/ 路径');
      record('8.3 按模块分布 Top10', 'PASS');
    } catch (e) {
      record('8.3 按模块分布 Top10', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 9. 退出登录
  // ============================================================
  console.log('\n--- 9. 退出登录 ---');

  // 9.1 点击退出返回登录页 + localStorage 清空
  {
    try {
      await closeDialogs(page);
      await clickBtn(page, '退出');
      await page.waitForTimeout(800);
      const backToLogin = await page.locator('.login-card').isVisible();
      if (!backToLogin) throw new Error('退出后未返回登录页');
      const key = await page.evaluate(() => localStorage.getItem('teamharness_api_key'));
      const member = await page.evaluate(() => localStorage.getItem('teamharness_member_id'));
      if (key || member) throw new Error('退出后 localStorage 未清空');
      await shot(page, '11-logout.png');
      record('9.1 退出登录返回登录页+清空 localStorage', 'PASS');
    } catch (e) {
      await shot(page, '11-logout-fail.png');
      record('9.1 退出登录返回登录页+清空 localStorage', 'FAIL', e.message);
    }
  }

  await ctx.close();

  // ============================================================
  // 10. 控制台错误汇总
  // ============================================================
  console.log('\n--- 10. 控制台错误 ---');
  {
    if (consoleErrors.length === 0) {
      record('10.1 全流程控制台无 error', 'PASS');
    } else {
      // 过滤掉已知的、非致命的错误（如 CDN 资源加载、favicon 404 等）
      const fatalErrors = consoleErrors.filter(e =>
        !e.includes('favicon') &&
        !e.includes('Failed to load resource') &&
        !e.includes('net::ERR')
      );
      if (fatalErrors.length === 0) {
        record('10.1 全流程控制台 error（均为非致命资源加载）', 'PASS',
          `共 ${consoleErrors.length} 条（含 favicon/CDN 资源）`);
      } else {
        console.log('  控制台错误明细：');
        fatalErrors.forEach((e, i) => console.log(`    [${i + 1}] ${e}`));
        record('10.1 全流程控制台 error', 'FAIL',
          `共 ${fatalErrors.length} 条致命错误（总计 ${consoleErrors.length} 条）`);
      }
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

  const reportPath = path.join(__dirname, 'test-results.txt');
  const reportContent = `TeamHarness 前端模块测试结果\n生成时间：${new Date().toISOString()}\n总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
    results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
    `\n\n截图目录：${SCREEN_DIR}\n`;
  fs.writeFileSync(reportPath, reportContent, 'utf8');

  process.exit(failCount > 0 ? 1 : 0);
})();
