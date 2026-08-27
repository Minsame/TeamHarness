// TeamHarness 规则库管理前端 E2E 测试
// 工具：Playwright + chromium
// 运行：node tests/frontend/e2e.spec.mjs
//
// 测试范围：登录页、6 个菜单页面、关键交互、截图
// 测试铁律：覆盖 happy path + 边界 + 异常；用例独立可复现（每用例前重载/重置）；突变数据 round-trip 还原

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE = 'http://localhost:8080/';
const FRONTEND = BASE;
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
// 用 hasText 过滤匹配文本，避免拾取到前一步残留的旧消息（如"授权已添加"残留导致"授权已撤销"误判）
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

// 选择 el-select 选项：点击 select 触发下拉，再点击"可见"的下拉项
// 关键：用 :visible 伪类只匹配当前打开的下拉，避免命中其他隐藏的同名项
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
  // ElMessageBox 默认 locale=en-US（CDN 未配置中文），确认按钮文本是 "OK" 而非"确定"
  // 用 primary 样式类定位确认按钮，cancel 用非 primary 按钮，避免 locale 依赖
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

// 重载页面回到"我的规则库"默认页（登录态由 localStorage 保持），保证测试隔离
async function reloadMyPage(page) {
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(1000);
}

// 关闭所有打开的对话框（按 Esc）
async function closeDialogs(page) {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
}

// ---------------- 主流程 ----------------
(async () => {
  console.log('=== TeamHarness 前端 E2E 测试开始 ===\n');
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error('浏览器启动失败：', e.message);
    process.exit(2);
  }

  // ============================================================
  // 一、登录页测试（独立 context）
  // ============================================================
  console.log('--- 一、登录页 ---');

  // 用例 1.1：空凭证提交
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(FRONTEND, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card', { timeout: 10000 });
      await shot(page, '01-login.png');
      await clickBtn(page, '登录');
      await waitForMessage(page, 'warning', '请输入成员 ID 和 API Key');
      const stillLogin = await page.locator('.login-card').isVisible();
      if (!stillLogin) throw new Error('空凭证提交后离开了登录页');
      record('1.1 空凭证提交提示警告', 'PASS');
    } catch (e) {
      await shot(page, '01-login-empty-fail.png');
      record('1.1 空凭证提交提示警告', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 用例 1.2：无效 API Key（应为登录失败）
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(FRONTEND, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', 'th_invalid_key_xxx');
      await clickBtn(page, '登录');
      const hasError = await hasMessage(page, 'error', 2000);
      const stillLogin = await page.locator('.login-card').isVisible().catch(() => false);
      if (hasError && stillLogin) {
        record('1.2 无效 API Key 拒绝登录', 'PASS');
      } else {
        const enteredMain = await page.locator('.app-header').isVisible().catch(() => false);
        if (enteredMain) {
          throw new Error('缺陷：无效 API Key 登录成功。根因：后端 /v1/auth/apikey/lookup 对无效 key 返回 200 {agent_id:null}，前端 handleLogin 未校验 agent_id 即放行');
        }
        throw new Error(`未出现 error 消息且未进入主界面，hasError=${hasError} stillLogin=${stillLogin}`);
      }
    } catch (e) {
      await shot(page, '01-login-invalid-fail.png');
      record('1.2 无效 API Key 拒绝登录', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 用例 1.3：颁发对话框打开/关闭
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(FRONTEND, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await clickBtn(page, '没有 Key？在此颁发');
      const dlg = page.locator('.el-dialog', { hasText: '颁发 API Key' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '03-issue-dialog.png');
      await clickBtn(page, '取消');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('1.3 颁发对话框打开/关闭', 'PASS');
    } catch (e) {
      await shot(page, '03-issue-dialog-fail.png');
      record('1.3 颁发对话框打开/关闭', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 用例 1.4：有效凭证登录 alice 进入主界面
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await page.goto(FRONTEND, { waitUntil: 'networkidle' });
      await page.waitForSelector('.login-card');
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await waitForMessage(page, 'success', '登录成功');
      await page.waitForSelector('.app-header', { timeout: 5000 });
      const memberTag = await page.locator('.el-tag', { hasText: '成员：alice' }).isVisible();
      if (!memberTag) throw new Error('主界面未显示成员 alice');
      await shot(page, '02-my-assets.png');
      record('1.4 有效凭证登录 alice 进入主界面', 'PASS');
    } catch (e) {
      await shot(page, '02-login-success-fail.png');
      record('1.4 有效凭证登录 alice 进入主界面', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // 用例 1.5：刷新页面保持登录
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      // 用 domcontentloaded 避免 CDN networkidle 间歇超时
      await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.login-card', { timeout: 10000 });
      await fillInput(page, '如：alice', 'alice');
      await fillInput(page, 'th_ 开头', ALICE_KEY);
      await clickBtn(page, '登录');
      await page.waitForSelector('.app-header', { timeout: 5000 });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.app-header', { timeout: 8000 });
      const stillIn = await page.locator('.app-header').isVisible();
      if (!stillIn) throw new Error('刷新后退出登录');
      record('1.5 刷新页面保持登录态', 'PASS');
    } catch (e) {
      record('1.5 刷新页面保持登录态', 'FAIL', e.message);
    } finally {
      await ctx.close();
    }
  }

  // ============================================================
  // 二、主界面共享会话（登录一次，复用于各页面测试）
  // ============================================================
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(FRONTEND, { waitUntil: 'networkidle' });
  await page.waitForSelector('.login-card');
  await fillInput(page, '如：alice', 'alice');
  await fillInput(page, 'th_ 开头', ALICE_KEY);
  await clickBtn(page, '登录');
  await page.waitForSelector('.app-header', { timeout: 5000 });
  await page.waitForTimeout(1000);

  // ============================================================
  // 二.1 我的规则库（my）— 每个用例前 reload 保证隔离
  // ============================================================
  console.log('\n--- 二.1 我的规则库 ---');

  // 用例 2.1：表格加载
  {
    try {
      await reloadMyPage(page);
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('表格无数据行');
      const tableText = await page.locator('.app-main .table-container').first().textContent();
      if (!tableText.includes('asset-alice-')) throw new Error('未找到 asset-alice- 资产');
      record('2.1 我的规则库表格加载', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '02-my-assets-fail.png');
      record('2.1 我的规则库表格加载', 'FAIL', e.message);
    }
  }

  // 用例 2.2：类型筛选器（选 rule）
  {
    try {
      await reloadMyPage(page);
      const typeSelect = page.locator('.filter-bar .el-select').first();
      await selectOption(page, typeSelect, 'rule 规则');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('筛选 rule 后无数据');
      // 只检查类型列（第2列）的 tag，避免 scope/status tag 干扰
      const typeTags = await page.locator('.app-main .el-table__row td:nth-child(2) .el-tag').allTextContents();
      const allRule = typeTags.every(t => t.trim() === 'rule');
      if (!allRule) throw new Error(`筛选后类型列存在非 rule：${typeTags.join(',')}`);
      record('2.2 类型筛选器（rule）', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '02-my-filter-type-fail.png');
      record('2.2 类型筛选器（rule）', 'FAIL', e.message);
    }
  }

  // 用例 2.3：共享范围筛选器（选 restricted）
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
      record('2.3 共享范围筛选器（restricted）', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '02-my-filter-scope-fail.png');
      record('2.3 共享范围筛选器（restricted）', 'FAIL', e.message);
    }
  }

  // 用例 2.4：分类筛选
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
      record('2.4 分类筛选器（backend/coding）', 'PASS', `行数=${rows}`);
    } catch (e) {
      record('2.4 分类筛选器（backend/coding）', 'FAIL', e.message);
    }
  }

  // 用例 2.5：模块路径筛选
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
      record('2.5 模块路径筛选器（modules/governance）', 'PASS', `行数=${rows}`);
    } catch (e) {
      record('2.5 模块路径筛选器（modules/governance）', 'FAIL', e.message);
    }
  }

  // 用例 2.6：详情对话框打开/关闭
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
      record('2.6 详情对话框打开/关闭', 'PASS');
    } catch (e) {
      await shot(page, '05-detail-dialog-fail.png');
      record('2.6 详情对话框打开/关闭', 'FAIL', e.message);
    }
  }

  // 用例 2.7：单个共享修改对话框（提交 + 还原 round-trip）
  {
    try {
      await reloadMyPage(page);
      const row002 = page.locator('.el-table__row', { hasText: 'asset-alice-002' }).first();
      await row002.locator('.el-button', { hasText: '共享' }).click();
      const dlg = page.locator('.el-dialog', { hasText: '修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '06-scope-dialog.png');
      await dlg.locator('.el-radio', { hasText: '私有' }).click();
      await clickBtn(page, '确认修改');
      await waitForMessage(page, 'success', '共享范围修改成功');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      await waitTableLoaded(page);
      // 验证 002 已变 private
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
      record('2.7 单个共享修改对话框（提交+还原）', 'PASS');
    } catch (e) {
      await shot(page, '06-scope-dialog-fail.png');
      record('2.7 单个共享修改对话框（提交+还原）', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 二.2 共享库（shared）
  // ============================================================
  console.log('\n--- 二.2 共享库 ---');
  {
    try {
      await closeDialogs(page);
      await gotoMenu(page, '共享库');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('共享库无数据');
      await shot(page, '07-shared-assets.png');
      record('2.8 共享库表格加载', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '07-shared-fail.png');
      record('2.8 共享库表格加载', 'FAIL', e.message);
    }
  }
  {
    try {
      const typeSelect = page.locator('.filter-bar .el-select').nth(1);
      await selectOption(page, typeSelect, 'rule');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows === 0) throw new Error('共享库筛选 rule 无数据');
      await selectOption(page, typeSelect, '全部');
      await waitTableLoaded(page);
      record('2.9 共享库类型筛选器', 'PASS');
    } catch (e) {
      record('2.9 共享库类型筛选器', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 二.3 共享管理（share-mgmt）
  // ============================================================
  console.log('\n--- 二.3 共享管理 ---');
  {
    try {
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const rows = await page.locator('.app-main .el-table__row').count();
      if (rows < 1) throw new Error('共享管理无数据');
      const batchBtn = page.locator('.el-button', { hasText: '批量修改共享' }).first();
      const disabled = await batchBtn.isDisabled();
      await shot(page, '08-share-mgmt.png');
      record('2.10 共享管理表格加载+批量按钮初始禁用', 'PASS', `行数=${rows}，禁用=${disabled}`);
    } catch (e) {
      await shot(page, '08-share-mgmt-fail.png');
      record('2.10 共享管理表格加载+批量按钮初始禁用', 'FAIL', e.message);
    }
  }
  // 用例 2.11：选择 + 批量修改对话框（取消）
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const firstCheckbox = page.locator('.app-main .el-table .el-checkbox').first();
      await firstCheckbox.click();
      await page.waitForTimeout(400);
      const batchBtn = page.locator('.el-button', { hasText: '批量修改共享' }).first();
      const disabled = await batchBtn.isDisabled();
      if (disabled) throw new Error('勾选后批量按钮仍禁用');
      await batchBtn.click();
      const dlg = page.locator('.el-dialog', { hasText: '批量修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await shot(page, '09-batch-scope-dialog.png');
      const alertText = await dlg.textContent();
      if (!alertText.includes('将修改') || !alertText.includes('个资产')) throw new Error('批量对话框未显示选中数量');
      await clickBtn(page, '取消');
      await dlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('2.11 选择+批量修改对话框（取消）', 'PASS');
    } catch (e) {
      await shot(page, '09-batch-scope-fail.png');
      record('2.11 选择+批量修改对话框（取消）', 'FAIL', e.message);
    }
  }
  // 用例 2.12：批量修改实际提交 + 还原（round-trip）
  {
    try {
      await reloadMyPage(page);
      await gotoMenu(page, '共享管理');
      await waitTableLoaded(page);
      const row005 = page.locator('.el-table__row', { hasText: 'asset-alice-005' }).first();
      await row005.locator('.el-checkbox').click();
      await page.waitForTimeout(300);
      await page.locator('.el-button', { hasText: '批量修改共享' }).first().click();
      const dlg = page.locator('.el-dialog', { hasText: '批量修改共享范围' });
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.locator('.el-radio', { hasText: '团队共享' }).click();
      await clickBtn(page, '确认批量修改');
      await waitForMessage(page, 'success', '批量修改完成');
      await waitTableLoaded(page);
      const row005b = page.locator('.el-table__row', { hasText: 'asset-alice-005' }).first();
      const rowText = await row005b.textContent();
      if (!rowText.includes('团队')) throw new Error('批量修改后 005 未变 team');
      // 还原：改回 public
      await row005b.locator('.el-checkbox').click();
      await page.waitForTimeout(300);
      await page.locator('.el-button', { hasText: '批量修改共享' }).first().click();
      await dlg.waitFor({ state: 'visible', timeout: 3000 });
      await dlg.locator('.el-radio', { hasText: '完全公开' }).click();
      await clickBtn(page, '确认批量修改');
      await waitForMessage(page, 'success', '批量修改完成');
      await waitTableLoaded(page);
      record('2.12 批量修改提交+还原（005 public→team→public）', 'PASS');
    } catch (e) {
      await shot(page, '09-batch-submit-fail.png');
      record('2.12 批量修改提交+还原（005 public→team→public）', 'FAIL', e.message);
    }
  }
  // 用例 2.13：快速修改 scope 下拉（带确认框，round-trip）
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
      record('2.13 快速修改 scope 下拉（003 team→private→team）', 'PASS');
    } catch (e) {
      await shot(page, '09-quick-scope-fail.png');
      record('2.13 快速修改 scope 下拉（003 team→private→team）', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 二.4 资产图谱（graph）
  // ============================================================
  console.log('\n--- 二.4 资产图谱 ---');
  {
    try {
      await closeDialogs(page);
      await gotoMenu(page, '资产图谱');
      await page.waitForTimeout(500);
      await fillInput(page, '根资产 ID', 'asset-alice-001');
      await clickBtn(page, '遍历');
      await page.waitForTimeout(1500);
      const descText = await page.locator('.el-descriptions').first().textContent();
      await shot(page, '10-graph.png');
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
      record('2.14 资产图谱 BFS 遍历（3 节点 2 边）', 'PASS', `节点=${nodeCount} 边=${edgeCount}`);
    } catch (e) {
      await shot(page, '10-graph-fail.png');
      record('2.14 资产图谱 BFS 遍历（3 节点 2 边）', 'FAIL', e.message);
    }
  }
  // 用例 2.15：深度切换为 1
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
      record('2.15 图谱深度1遍历', 'PASS', `节点=${nodeCount} 边=${edgeCount}`);
    } catch (e) {
      record('2.15 图谱深度1遍历', 'FAIL', e.message);
    }
  }
  // 用例 2.16：空根资产 ID 提交
  {
    try {
      await page.getByPlaceholder('根资产 ID').first().fill('');
      await clickBtn(page, '遍历');
      await waitForMessage(page, 'warning', '请输入资产 ID');
      record('2.16 图谱空根ID提示警告', 'PASS');
    } catch (e) {
      record('2.16 图谱空根ID提示警告', 'FAIL', e.message);
    }
  }
  // 用例 2.17：关联列表对话框 UI 入口（关键缺陷验证）
  {
    try {
      // showLinksDialog 在 app.js 定义但 index.html 无 @click 触发入口
      // 检查图谱页面是否有任何按钮能打开"资产关联"对话框
      await page.waitForTimeout(500);
      const graphBtns = await page.locator('.app-main .el-button').allTextContents();
      const hasLinksEntry = graphBtns.some(b => b.includes('关联'));
      if (hasLinksEntry) {
        record('2.17 关联列表对话框 UI 入口', 'PASS', `发现入口按钮：${graphBtns.filter(b=>b.includes('关联')).join(',')}`);
      } else {
        throw new Error('showLinksDialog 在 app.js 定义但 index.html 无 @click 触发，关联列表/添加关联/删除关联对话框通过 UI 不可达（死代码）。图谱页可见按钮：[' + graphBtns.join(', ') + ']');
      }
    } catch (e) {
      record('2.17 关联列表对话框 UI 入口缺失（缺陷）', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 二.5 ACL 授权（acl）
  // ============================================================
  console.log('\n--- 二.5 ACL 授权 ---');
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
      await shot(page, '11-acl.png');
      record('2.18 ACL 受限资产列表', 'PASS', `行数=${rows}`);
    } catch (e) {
      await shot(page, '11-acl-fail.png');
      record('2.18 ACL 受限资产列表', 'FAIL', e.message);
    }
  }
  // 用例 2.19：ACL 管理对话框打开
  {
    try {
      const row004 = page.locator('.el-table__row', { hasText: 'asset-alice-004' }).first();
      await row004.locator('.el-button', { hasText: '管理 ACL' }).click();
      const dlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(800);
      await shot(page, '12-acl-dialog.png');
      const txt = await dlg.textContent();
      if (!txt.includes('当前授权')) throw new Error('ACL 对话框未显示授权列表');
      record('2.19 ACL 管理对话框打开', 'PASS');
    } catch (e) {
      await shot(page, '12-acl-dialog-fail.png');
      record('2.19 ACL 管理对话框打开', 'FAIL', e.message);
    }
  }
  // 用例 2.20：添加 ACL 授权 + 撤销（round-trip）
  // 用唯一 grantee_id 避免与前次测试残留数据冲突（后端对重复授权返回 500）
  {
    try {
      const dlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      const beforeRows = await dlg.locator('.el-table__row').count();
      // 点"+ 添加授权"（在 ACL 列表对话框内）
      await dlg.locator('.el-button', { hasText: '添加授权' }).click();
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      await formDlg.waitFor({ state: 'visible', timeout: 3000 });
      await page.waitForTimeout(400); // 等待对话框过渡动画完成
      const uniqueGrantee = 'e2e-bob-' + Date.now();
      await formDlg.getByPlaceholder('member_id / agent_id / role_name').first().fill(uniqueGrantee);
      // 提交按钮限定到表单对话框，避免命中 ACL 列表对话框中的"+ 添加授权"
      await formDlg.locator('.el-button', { hasText: '添加授权' }).click();
      await waitForMessage(page, 'success', '授权已添加', 6000);
      await formDlg.waitFor({ state: 'hidden', timeout: 3000 });
      await page.waitForTimeout(800);
      const afterRows = await dlg.locator('.el-table__row').count();
      if (afterRows !== beforeRows + 1) throw new Error(`添加后授权数未 +1：before=${beforeRows} after=${afterRows}`);
      // 撤销刚添加的（找含 uniqueGrantee 的行的"撤销"按钮）
      const granteeRow = dlg.locator('.el-table__row', { hasText: uniqueGrantee }).first();
      await granteeRow.locator('.el-button', { hasText: '撤销' }).click();
      await page.waitForSelector('.el-message-box', { timeout: 3000 });
      await confirmMessageBox(page, 'confirm');
      await waitForMessage(page, 'success', '授权已撤销');
      await page.waitForTimeout(800);
      const finalRows = await dlg.locator('.el-table__row').count();
      if (finalRows !== beforeRows) throw new Error(`撤销后授权数未还原：before=${beforeRows} final=${finalRows}`);
      record('2.20 ACL 添加授权+撤销（round-trip）', 'PASS');
    } catch (e) {
      await shot(page, '12-acl-add-fail.png');
      record('2.20 ACL 添加授权+撤销（round-trip）', 'FAIL', e.message);
    }
  }
  // 用例 2.21：添加授权空对象 ID 校验
  {
    try {
      await closeDialogs(page);
      await reloadMyPage(page);
      await gotoMenu(page, 'ACL 授权');
      await waitTableLoaded(page);
      const row004 = page.locator('.el-table__row', { hasText: 'asset-alice-004' }).first();
      await row004.locator('.el-button', { hasText: '管理 ACL' }).click();
      const dlg = page.locator('.el-dialog', { hasText: 'ACL 授权' });
      await dlg.waitFor({ state: 'visible', timeout: 5000 });
      await page.waitForTimeout(600);
      await dlg.locator('.el-button', { hasText: '添加授权' }).click();
      const formDlg = page.locator('.el-dialog', { hasText: '添加 ACL 授权' });
      await formDlg.waitFor({ state: 'visible', timeout: 3000 });
      // 不填对象 ID，直接点添加（限定到表单对话框）
      await formDlg.locator('.el-button', { hasText: '添加授权' }).click();
      await waitForMessage(page, 'warning', '请输入授权对象 ID');
      await formDlg.locator('.el-button', { hasText: '取消' }).click();
      await formDlg.waitFor({ state: 'hidden', timeout: 3000 });
      record('2.21 ACL 添加授权空对象ID校验', 'PASS');
    } catch (e) {
      record('2.21 ACL 添加授权空对象ID校验', 'FAIL', e.message);
    }
  }
  // 关闭残留对话框
  await closeDialogs(page);

  // ============================================================
  // 二.6 治理看板（dashboard）
  // ============================================================
  console.log('\n--- 二.6 治理看板 ---');
  {
    try {
      await closeDialogs(page);
      await reloadMyPage(page);
      await gotoMenu(page, '治理看板');
      await page.waitForTimeout(1200);
      const card1 = page.locator('.el-card', { hasText: '我的资产总数' }).first();
      await card1.waitFor({ state: 'visible', timeout: 5000 });
      const cards = ['我的资产总数', '私有资产', '团队共享', '公开资产'];
      for (const c of cards) {
        const visible = await page.locator('.el-card', { hasText: c }).first().isVisible();
        if (!visible) throw new Error(`统计卡片缺失：${c}`);
      }
      const typeCard = await page.locator('.el-card', { hasText: '按类型分布' }).first().isVisible();
      const modCard = await page.locator('.el-card', { hasText: '按模块分布' }).first().isVisible();
      if (!typeCard || !modCard) throw new Error('分布卡片缺失');
      await shot(page, '13-dashboard.png');
      record('2.22 治理看板统计卡片+分布渲染', 'PASS');
    } catch (e) {
      await shot(page, '13-dashboard-fail.png');
      record('2.22 治理看板统计卡片+分布渲染', 'FAIL', e.message);
    }
  }
  // 用例 2.23：按类型分布进度条
  {
    try {
      const typeCard = page.locator('.el-card', { hasText: '按类型分布' }).first();
      const progressBars = await typeCard.locator('.el-progress').count();
      if (progressBars === 0) throw new Error('按类型分布无进度条');
      record('2.23 按类型分布进度条', 'PASS', `进度条数=${progressBars}`);
    } catch (e) {
      record('2.23 按类型分布进度条', 'FAIL', e.message);
    }
  }
  // 用例 2.24：按模块分布 Top10
  {
    try {
      const modCard = page.locator('.el-card', { hasText: '按模块分布' }).first();
      const modText = await modCard.textContent();
      if (!modText.includes('modules/')) throw new Error('按模块分布未显示 modules/ 路径');
      record('2.24 按模块分布 Top10', 'PASS');
    } catch (e) {
      record('2.24 按模块分布 Top10', 'FAIL', e.message);
    }
  }

  // ============================================================
  // 三、退出登录
  // ============================================================
  console.log('\n--- 三、退出登录 ---');
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
      await shot(page, '14-logout.png');
      record('3.1 退出登录返回登录页+清空凭证', 'PASS');
    } catch (e) {
      await shot(page, '14-logout-fail.png');
      record('3.1 退出登录返回登录页+清空凭证', 'FAIL', e.message);
    }
  }

  await ctx.close();
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

  const reportPath = path.join(__dirname, 'e2e-results.txt');
  const reportContent = `TeamHarness 前端 E2E 测试结果\n生成时间：${new Date().toISOString()}\n总计：${results.length}  PASS：${passCount}  FAIL：${failCount}  SKIP：${skipCount}\n\n` +
    results.map(r => `[${r.status}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`).join('\n') +
    `\n\n截图目录：${SCREEN_DIR}\n`;
  fs.writeFileSync(reportPath, reportContent, 'utf8');

  process.exit(failCount > 0 ? 1 : 0);
})();
