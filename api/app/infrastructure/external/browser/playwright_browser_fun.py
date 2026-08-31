#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : playwright_browser_fun.py
工业级DOM浏览器JS注入函数 - Shadow DOM穿透、语义化元素定位、SPA适配、阻塞元素检测
"""

GET_VISIBLE_CONTENT_FUNC = """() => {
    const viewportH = window.innerHeight;
    const viewportW = window.innerWidth;
    const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','PATH','META','LINK','HEAD']);
    // 注意: 不跳过role="navigation" — 侧边栏导航链接(如"Form 表单")是LLM操作页面的关键信息,
    // 跳过navigation会导致LLM在content中找不到导航链接→误判"内容为空"→滥用console_exec/browser_restart。
    // 仅跳过banner(页头logo/搜索)和contentinfo(页脚版权),保留navigation和complementary。
    const SKIP_ROLES = new Set(['banner','contentinfo']);
    const MAX_DEPTH = 15;

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        const s = window.getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    }

    function inViewport(el) {
        const r = el.getBoundingClientRect();
        return !(r.bottom < 0 || r.top > viewportH || r.right < 0 || r.left > viewportW);
    }

    function extractText(el) {
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
            let parts = [];
            if (el.id) {
                const label = document.querySelector('label[for="' + el.id + '"]');
                if (label) parts.push('[Label:' + label.innerText.trim() + ']');
            }
            const parentLabel = el.closest('label');
            if (parentLabel) parts.push('[Label:' + parentLabel.innerText.trim().replace(el.value||'','').trim() + ']');
            if (el.placeholder) parts.push('[Placeholder:' + el.placeholder + ']');
            if (el.value) parts.push('[Value:' + el.value + ']');
            if (el.type) parts.push('[' + el.type + ']');
            return parts.join(' ') || '[' + el.tagName.toLowerCase() + ']';
        }
        const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
        return text.length > 400 ? text.substring(0, 397) + '...' : text;
    }

    function getSemanticAttrs(el) {
        const attrs = {};
        for (const name of ['aria-label','aria-role','role','data-testid','data-test','name','title','alt','href','type','placeholder']) {
            const val = el.getAttribute(name);
            if (val) attrs[name] = val;
        }
        return Object.keys(attrs).length > 0 ? attrs : null;
    }

    function getChildElements(root) {
        const children = [];
        if (root.children) {
            for (const child of root.children) children.push(child);
        }
        if (root.shadowRoot) {
            for (const child of root.shadowRoot.children) children.push(child);
        }
        return children;
    }

    function walk(el, depth) {
        if (!el || depth > MAX_DEPTH) return null;
        const tagName = el.tagName;
        if (!tagName || SKIP_TAGS.has(tagName)) return null;
        if (!isVisible(el)) return null;

        const role = el.getAttribute('role');
        if (role && SKIP_ROLES.has(role)) return null;

        const tag = tagName.toLowerCase();
        // 扩展交互判定: 覆盖ARIA角色 + Element UI/Ant Design/Naive UI组件 + [onclick]
        // 与GET_INTERACTIVE_ELEMENTS_FUNC的SELECTOR保持一致
        const isInteractive = el.matches(
            'a,button,input,textarea,select,' +
            '[role="button"],[role="menuitem"],[role="tab"],' +
            '[role="option"],[role="treeitem"],[role="link"],' +
            '[tabindex]:not([tabindex="-1"]),' +
            '[onclick],' +
            '.el-menu-item,.el-tabs__item,.el-button,.el-link,' +
            '.el-tree-node__content,.el-radio,.el-checkbox,' +
            '.el-select,.el-cascader,.el-switch,.el-tag,' +
            '.ant-btn,.ant-menu-item,.ant-tabs-tab,' +
            '.n-button,.n-menu-item'
        );
        const hasText = (el.innerText || '').trim().length > 0;
        const isImg = tag === 'img';
        const isViewport = inViewport(el);

        // 注意: 不在此处提前过滤无文本/非交互/非图片的元素,
        // 因为它们可能是包含子内容的包装div(VitePress/Vue SPA常见结构)。
        // 先遍历子元素,再根据子元素是否存在决定是否跳过(见下方late check)。

        const children = [];
        for (const child of getChildElements(el)) {
            const node = walk(child, depth + 1);
            if (node) children.push(node);
        }

        // 同源iframe穿透:递归进入iframe的contentDocument
        if (tag === 'iframe') {
            try {
                const iframeDoc = el.contentDocument;
                if (iframeDoc && iframeDoc.body) {
                    const iframeNode = walk(iframeDoc.body, depth + 1);
                    if (iframeNode) children.push({tag: 'iframe-content', children: [iframeNode]});
                }
            } catch(e) {}  // 跨域iframe访问contentDocument会抛SecurityError,自动跳过
        }

        // Late check: 遍历子元素后,仅当元素无交互性、无子内容、无文本、非图片时才跳过
        // 这确保包装div(无直接文本但包含子元素文本)不会被错误跳过
        if (!isInteractive && children.length === 0 && !hasText && !isImg) return null;

        const result = { tag };
        const semanticAttrs = getSemanticAttrs(el);
        if (semanticAttrs) result.attrs = semanticAttrs;
        if (isInteractive) result.interactive = true;
        if (!isViewport) result.offscreen = true;

        const directText = extractText(el);
        if (directText && (isInteractive || children.length === 0)) {
            result.text = directText;
        }

        if (children.length > 0) result.children = children;
        return result;
    }

    const root = walk(document.body, 0);
    return root ? JSON.stringify(root) : '{}';
}"""

GET_INTERACTIVE_ELEMENTS_FUNC = """() => {
    const viewportH = window.innerHeight;
    const viewportW = window.innerWidth;
    // 扩展选择器: 覆盖ARIA角色 + Element UI/Ant Design/Naive UI组件 + [onclick] + cursor:pointer
    // 会话34af4e8d/3c4debd1暴露: 基础选择器漏掉el-menu-item/el-tabs__item等UI框架组件,
    // 导致LLM看不到交互元素→滥用console_exec查找元素
    const SELECTOR = [
        'a', 'button', 'input', 'textarea', 'select',
        '[role="button"]', '[role="menuitem"]', '[role="tab"]',
        '[role="option"]', '[role="treeitem"]', '[role="link"]',
        '[tabindex]:not([tabindex="-1"])',
        '[onclick]',
        // Element UI 组件
        '.el-menu-item', '.el-tabs__item', '.el-button', '.el-link',
        '.el-tree-node__content', '.el-radio', '.el-checkbox',
        '.el-select', '.el-cascader', '.el-switch', '.el-tag',
        // Ant Design 组件
        '.ant-btn', '.ant-menu-item', '.ant-tabs-tab',
        // Naive UI 组件
        '.n-button', '.n-menu-item'
    ].join(',');

    function queryAllDeep(root, selector) {
        const elements = [];
        try {
            const found = root.querySelectorAll(selector);
            for (const el of found) elements.push(el);
        } catch(e) {}
        try {
            const all = root.querySelectorAll('*');
            for (const el of all) {
                if (el.shadowRoot) {
                    const shadowElements = queryAllDeep(el.shadowRoot, selector);
                    for (const se of shadowElements) elements.push(se);
                }
            }
        } catch(e) {}
        return elements;
    }

    // 同源iframe穿透:遍历iframe的contentDocument,跨域iframe自动跳过
    function collectFromIframes(root, selector) {
        const elements = [];
        try {
            const iframes = root.querySelectorAll('iframe');
            for (const iframe of iframes) {
                try {
                    const iframeDoc = iframe.contentDocument;
                    if (!iframeDoc) continue;
                    const iframeElements = queryAllDeep(iframeDoc, selector);
                    for (const el of iframeElements) {
                        elements.push({el: el, inIframe: true});
                    }
                    // 嵌套iframe递归
                    const nested = collectFromIframes(iframeDoc, selector);
                    for (const el of nested) elements.push(el);
                } catch(e) {}  // 跨域iframe访问contentDocument会抛SecurityError
            }
        } catch(e) {}
        return elements;
    }

    let idx = 0;
    const results = [];

    const allInteractive = queryAllDeep(document, SELECTOR);
    const iframeInteractive = collectFromIframes(document, SELECTOR);

    // cursor:pointer补充扫描: 捕获CSS驱动的可点击元素(如自定义按钮、图标按钮)
    // 这些元素可能没有语义化标签或ARIA角色,但通过cursor:pointer表明可交互
    // 仅扫描viewport内元素,避免遍历全量DOM影响性能
    const cursorPointerElements = [];
    try {
        const viewportEls = document.querySelectorAll('div,span,i,svg,img,li');
        for (const el of viewportEls) {
            if (allInteractive.indexOf(el) !== -1) continue;  // 已收集,跳过
            const s = window.getComputedStyle(el);
            if (s.cursor === 'pointer') {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < viewportH) {
                    cursorPointerElements.push(el);
                }
            }
        }
    } catch(e) {}

    // 表单项标签提取: 覆盖主流UI框架(element-plus/ant-design/naive)与原生表单结构。
    // 解决空输入框无标签上下文→LLM无法区分表单字段→滥用console_exec排查的根因(会话6b8e1a36)。
    // 返回标签文本(无标签返回空串),调用方负责拼装[Label:xxx]前缀。
    function getFormItemLabel(el) {
        // 1.element-plus: .el-form-item > .el-form-item__label
        const epItem = el.closest('.el-form-item');
        if (epItem) {
            const lbl = epItem.querySelector('.el-form-item__label');
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
        }
        // 2.ant-design: .ant-form-item > .ant-form-item-label > label
        const antItem = el.closest('.ant-form-item');
        if (antItem) {
            const lbl = antItem.querySelector('.ant-form-item-label > label');
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
        }
        // 3.naive-ui / 通用: 祖先[role=group][aria-label]或[aria-labelledby]
        const grp = el.closest('[role="group"][aria-label]');
        if (grp && grp.getAttribute('aria-label')) return grp.getAttribute('aria-label').trim();
        // 4.fieldset > legend(原生表单分组)
        const fs = el.closest('fieldset');
        if (fs) {
            const lg = fs.querySelector('legend');
            if (lg && lg.innerText.trim()) return lg.innerText.trim();
        }
        // 5.原生label[for=id] / 包裹式label
        if (el.id) {
            const l = document.querySelector('label[for="'+el.id+'"]');
            if (l && l.innerText.trim()) return l.innerText.trim();
        }
        const pl = el.closest('label');
        if (pl && pl.innerText.trim()) return pl.innerText.trim();
        return '';
    }

    function processElement(el, inIframe) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;

        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return;

        const isInViewport = !(r.bottom < 0 || r.top > viewportH || r.right < 0 || r.left > viewportW);
        const tag = el.tagName.toLowerCase();

        let text = '';
        if (['input','textarea','select'].includes(tag)) {
            // 表单控件: 优先提取标签上下文(空输入框也能识别为"Name"字段),
            // 再拼装当前值/placeholder,避免LLM面对一堆无标签input滥用console_exec
            const label = getFormItemLabel(el);
            const parts = [];
            if (label) parts.push('[Label:'+label+']');
            if (el.value) parts.push('[Value:'+el.value+']');
            if (el.placeholder) parts.push('[Placeholder:'+el.placeholder+']');
            if (!parts.length) {
                // 无标签无值无placeholder: 用type兜底(text/password/checkbox等)
                text = el.type ? '['+el.type+']' : '[No text]';
            } else {
                text = parts.join(' ');
            }
        } else if (el.innerText) {
            text = el.innerText.trim().replace(/\\s+/g,' ');
        } else if (el.alt) {
            text = el.alt;
        } else if (el.title) {
            text = el.title;
        } else if (el.placeholder) {
            text = '[Placeholder:'+el.placeholder+']';
        } else if (el.type) {
            text = '['+el.type+']';
        } else {
            text = '[No text]';
        }
        if (text.length > 200) text = text.substring(0, 197) + '...';

        const manusId = 'manus-element-' + idx;
        try { el.setAttribute('data-manus-id', manusId); } catch(e) {}

        const semanticAttrs = {};
        for (const name of ['aria-label','role','name','title','href','type','placeholder','data-testid']) {
            const val = el.getAttribute(name);
            if (val) semanticAttrs[name] = val;
        }

        const isInShadow = el.getRootNode() !== document;
        // 标记元素是否位于弹窗内(el-dialog/el-drawer/ant-modal),供LLM优先操作弹窗元素
        const inDialog = !!(el.closest('.el-dialog, .el-drawer, .ant-modal, [role="dialog"][aria-modal="true"]'));

        // 表单元素状态: checked/selected/disabled(供LLM直接判断radio/checkbox选中态,
        // 减少console_exec调用。会话ab17bf13根因:LLM无法从view判断radio选中状态,
        // 被迫8次console_exec查询DOM)
        const elemState = {};
        if (tag === 'input' && (el.type === 'radio' || el.type === 'checkbox')) {
            elemState.checked = el.checked;
        }
        // UI框架radio/checkbox: input通常视觉隐藏(width/height=0被可见性过滤),
        // 实际捕获的是label内部span(经cursor:pointer扫描)。向上攀升到label容器,
        // 查询关联input的checked态,使span也能携带[checked]标记。
        // 覆盖element-plus(.el-radio-button/.el-radio/.el-checkbox)与ant-design(.ant-radio-wrapper等)
        else {
            const radioLabel = el.closest(
                '.el-radio-button, .el-radio, .el-checkbox, ' +
                '.ant-radio-button-wrapper, .ant-radio-wrapper, .ant-checkbox-wrapper, ' +
                '[role="radio"], [role="checkbox"]'
            );
            if (radioLabel) {
                const innerInput = radioLabel.querySelector('input[type="radio"], input[type="checkbox"]');
                if (innerInput && innerInput.checked) elemState.checked = true;
                if (radioLabel.classList.contains('is-disabled') || (innerInput && innerInput.disabled)) {
                    elemState.disabled = true;
                }
            }
        }
        if (tag === 'option') {
            elemState.selected = el.selected;
        }
        if (el.disabled && !elemState.disabled) {
            elemState.disabled = true;
        }

        const entry = {
            index: idx,
            tag: tag,
            text: text,
            selector: '[data-manus-id="'+manusId+'"]',
            inViewport: isInViewport,
            semanticAttrs: Object.keys(semanticAttrs).length > 0 ? semanticAttrs : null,
            inShadowDOM: isInShadow,
            inDialog: inDialog,
        };
        if (Object.keys(elemState).length > 0) entry.state = elemState;
        if (inIframe) entry.inIframe = true;
        results.push(entry);
        idx++;
    }

    for (const el of allInteractive) processElement(el, false);
    for (const item of iframeInteractive) processElement(item.el, true);
    for (const el of cursorPointerElements) processElement(el, false);

    return results;
}"""

INJECT_CONSOLE_LOGS_FUNC = """() => {
    if (window.__consoleLogs) return;
    window.__consoleLogs = [];
    const orig = { log: console.log, warn: console.warn, error: console.error };
    console.log = (...a) => { window.__consoleLogs.push({level:'log',msg:a.join(' ')}); orig.log.apply(console,a); };
    console.warn = (...a) => { window.__consoleLogs.push({level:'warn',msg:a.join(' ')}); orig.warn.apply(console,a); };
    console.error = (...a) => { window.__consoleLogs.push({level:'error',msg:a.join(' ')}); orig.error.apply(console,a); };
}"""

GET_PAGE_STATE_FUNC = """() => {
    // 检测应用弹窗(el-dialog/el-drawer/ant-modal),供LLM判断弹窗状态和滚动目标
    const dialogEl = document.querySelector(
        '.el-dialog__wrapper:not([style*="display: none"]) .el-dialog, ' +
        '.el-drawer__container:not([style*="display: none"]) .el-drawer, ' +
        '.ant-modal-wrap:not([style*="display: none"]) .ant-modal'
    );
    let dialogInfo = null;
    if (dialogEl) {
        const scrollBody = dialogEl.querySelector('.el-dialog__body, .el-drawer__body, .ant-modal-body') || dialogEl;
        const scrollInfo = scrollBody ? {
            scrollHeight: scrollBody.scrollHeight,
            clientHeight: scrollBody.clientHeight,
            scrollTop: scrollBody.scrollTop,
            canScrollDown: scrollBody.scrollHeight - scrollBody.scrollTop - scrollBody.clientHeight > 10,
            canScrollUp: scrollBody.scrollTop > 10,
        } : null;
        dialogInfo = {
            type: dialogEl.classList.contains('el-dialog') ? 'el-dialog' :
                  dialogEl.classList.contains('el-drawer') ? 'el-drawer' : 'modal',
            canScroll: scrollInfo && scrollInfo.canScrollDown,
            scrollInfo: scrollInfo,
        };
    }

    // 阻塞型覆盖层检测(仅cookie横幅和加载遮罩,不含应用弹窗)
    const blockingEl = document.querySelector(
        '[class*="cookie-banner"], [class*="consent-banner"], ' +
        '[class*="loading-mask"], [class*="spinner-overlay"]'
    );
    let hasBlocking = false;
    if (blockingEl) {
        const r = blockingEl.getBoundingClientRect();
        const s = window.getComputedStyle(blockingEl);
        hasBlocking = r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
    }
    return {
        url: window.location.href,
        title: document.title,
        readyState: document.readyState,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        scrollHeight: document.body.scrollHeight,
        viewportHeight: window.innerHeight,
        canScrollDown: (window.scrollY + window.innerHeight) < document.body.scrollHeight - 10,
        canScrollUp: window.scrollY > 10,
        activeElement: document.activeElement ? document.activeElement.tagName.toLowerCase() : null,
        hasDialog: !!dialogEl,
        dialogInfo: dialogInfo,
        hasBlockingElement: hasBlocking,
    };
}"""

WAIT_DOM_STABLE_FUNC = """(timeout) => {
    // 优先用MutationObserver监听子树变化,等待连续_STABLE_INTERVAL无mutation即认为稳定;
    // 巨型页面下比innerHTML全量字符串比对性能好几个数量级。
    // MutationObserver不可用时退回innerHTML比对(兼容极旧浏览器)。
    const _STABLE_INTERVAL = 300;  // 连续无mutation的稳定窗口(ms)
    return new Promise((resolve) => {
        const deadline = Date.now() + timeout;
        if (typeof MutationObserver === 'undefined') {
            // Fallback: innerHTML全量比对(原实现,保留向后兼容)
            let lastHTML = document.body.innerHTML;
            let stableCount = 0;
            const check = () => {
                if (Date.now() > deadline) { resolve(true); return; }
                const current = document.body.innerHTML;
                if (current === lastHTML) {
                    stableCount++;
                    if (stableCount >= 3) { resolve(true); return; }
                } else {
                    stableCount = 0;
                    lastHTML = current;
                }
                setTimeout(check, 200);
            };
            setTimeout(check, 200);
            return;
        }
        let lastMutationTime = Date.now();
        const observer = new MutationObserver(() => {
            lastMutationTime = Date.now();
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
        });
        const check = () => {
            const now = Date.now();
            if (now - lastMutationTime >= _STABLE_INTERVAL || now > deadline) {
                observer.disconnect();
                resolve(true);
                return;
            }
            setTimeout(check, 100);
        };
        setTimeout(check, _STABLE_INTERVAL);
    });
}"""

DETECT_BLOCKING_ELEMENTS_FUNC = """() => {
    // 仅检测阻塞型覆盖层(cookie横幅/加载遮罩),不检测应用弹窗(el-dialog/ant-modal)。
    // 应用弹窗是LLM需交互的内容,自动关闭会导致LLM无法读取弹窗信息而滥用console_exec。
    const categories = {
        cookie_banner: {
            selectors: ['[class*="cookie-banner"]','[class*="consent-banner"]',
                '[class*="gdpr"]','[id*="cookie"]','[class*="privacy-banner"]'],
            closeSelectors: ['button:has-text("Accept")','button:has-text("接受")',
                'button:has-text("同意")','button:has-text("OK")','[class*="accept"]','[class*="agree"]']
        },
        loading_overlay: {
            selectors: ['[class*="loading-mask"]','[class*="spinner-overlay"]',
                '.el-loading-mask','.ant-spin','[class*="skeleton"]'],
            closeSelectors: []
        }
    };

    const results = [];
    for (const [category, config] of Object.entries(categories)) {
        for (const sel of config.selectors) {
            try {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    if (r.width === 0 || r.height === 0 || s.display === 'none' || s.visibility === 'hidden') continue;
                    const cx = window.innerWidth / 2;
                    const cy = window.innerHeight / 2;
                    const isBlocking = r.left < cx && r.right > cx && r.top < cy && r.bottom > cy;
                    if (!isBlocking) continue;
                    results.push({
                        category: category,
                        selector: sel,
                        tagName: el.tagName.toLowerCase(),
                        hasCloseBtn: config.closeSelectors.length > 0,
                        closeSelectors: config.closeSelectors,
                    });
                    break;
                }
            } catch(e) {}
        }
    }
    return results;
}"""

DISMISS_BLOCKING_ELEMENT_FUNC = """(closeSelector) => {
    try {
        const btn = document.querySelector(closeSelector);
        if (btn) { btn.click(); return true; }
    } catch(e) {}
    return false;
}"""

CHECK_ELEMENT_INTERACTABLE_FUNC = """(el) => {
    if (!el) return { interactable: false, reason: 'null' };
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width === 0 || rect.height === 0)
        return { interactable: false, reason: 'zero_size' };
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')
        return { interactable: false, reason: 'hidden' };
    if (el.disabled)
        return { interactable: false, reason: 'disabled' };
    if (style.pointerEvents === 'none')
        return { interactable: false, reason: 'pointer_events_none' };
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    const topEl = document.elementFromPoint(centerX, centerY);
    if (topEl && topEl !== el && !el.contains(topEl) && !topEl.contains(el)) {
        return { interactable: false, reason: 'occluded', occluder: topEl.tagName };
    }
    return { interactable: true };
}"""

DISPATCH_CLICK_FUNC = """(el) => {
    const events = ['pointerdown','mousedown','pointerup','mouseup','click'];
    for (const type of events) {
        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }
    return true;
}"""

SCROLL_TO_TEXT_FUNC = """(text) => {
    const target = String(text || '').trim();
    if (!target) return null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node.textContent && node.textContent.indexOf(target) !== -1) {
            const el = node.parentElement;
            if (el) {
                // instant滚动: 立即完成,确保后续getBoundingClientRect准确反映最终位置。
                // smooth滚动会延迟完成,Python侧等待期间元素仍标记offscreen,
                // 导致LLM反复browser_view确认状态(会话252d3f44根因)。
                el.scrollIntoView({behavior: 'instant', block: 'center'});
                el.setAttribute('data-manus-scroll-target', 'true');
                setTimeout(() => el.removeAttribute('data-manus-scroll-target'), 2000);
                // 返回目标元素信息: 文本片段(让LLM确认匹配正确) + 视口可见性(减少额外browser_view)
                const rect = el.getBoundingClientRect();
                const inViewport = rect.top >= 0 && rect.bottom <= window.innerHeight
                    && rect.width > 0 && rect.height > 0;
                const textSnippet = (el.innerText || el.textContent || '').trim().substring(0, 100);
                return { found: true, text: textSnippet, inViewport: inViewport };
            }
        }
    }
    return null;
}"""

LOCATE_BY_SEMANTIC_FUNC = """(targetText) => {
    // 语义属性定位函数:作为Playwright locator API(get_by_role/get_by_text)的回退,
    // 处理图标按钮(aria-label)、提示输入框(placeholder)、悬停提示(title)等无可见文本场景。
    // 复用queryAllDeep实现Shadow DOM穿透,collectFromIframes实现同源iframe穿透。
    const target = String(targetText || '').trim();
    if (!target) return null;

    const SELECTOR = 'a,button,input,textarea,select,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="option"],[role="treeitem"],[tabindex]:not([tabindex="-1"])';

    function queryAllDeep(root, selector) {
        const elements = [];
        try {
            const found = root.querySelectorAll(selector);
            for (const el of found) elements.push(el);
        } catch(e) {}
        try {
            const all = root.querySelectorAll('*');
            for (const el of all) {
                if (el.shadowRoot) {
                    const shadowElements = queryAllDeep(el.shadowRoot, selector);
                    for (const se of shadowElements) elements.push(se);
                }
            }
        } catch(e) {}
        return elements;
    }

    // 同源iframe穿透:遍历iframe的contentDocument,跨域iframe自动跳过
    function collectFromIframes(root, selector) {
        const elements = [];
        try {
            const iframes = root.querySelectorAll('iframe');
            for (const iframe of iframes) {
                try {
                    const iframeDoc = iframe.contentDocument;
                    if (!iframeDoc) continue;
                    const iframeElements = queryAllDeep(iframeDoc, selector);
                    for (const el of iframeElements) elements.push(el);
                    const nested = collectFromIframes(iframeDoc, selector);
                    for (const el of nested) elements.push(el);
                } catch(e) {}
            }
        } catch(e) {}
        return elements;
    }

    const candidates = queryAllDeep(document, SELECTOR);
    const iframeCandidates = collectFromIframes(document, SELECTOR);
    const all = candidates.concat(iframeCandidates);

    // 表单项标签提取: 复用GET_INTERACTIVE_ELEMENTS_FUNC的getFormItemLabel逻辑,
    // 让text_locator="Name"能定位到element-plus/ant-design表单中对应标签的输入框,
    // 避免LLM因找不到输入框回退console_exec(会话6b8e1a36根因)。
    function getFormItemLabel(el) {
        const ep = el.closest('.el-form-item');
        if (ep) { const l = ep.querySelector('.el-form-item__label'); if (l && l.innerText.trim()) return l.innerText.trim(); }
        const ant = el.closest('.ant-form-item');
        if (ant) { const l = ant.querySelector('.ant-form-item-label > label'); if (l && l.innerText.trim()) return l.innerText.trim(); }
        const grp = el.closest('[role="group"][aria-label]');
        if (grp && grp.getAttribute('aria-label')) return grp.getAttribute('aria-label').trim();
        const fs = el.closest('fieldset');
        if (fs) { const lg = fs.querySelector('legend'); if (lg && lg.innerText.trim()) return lg.innerText.trim(); }
        if (el.id) { const l = document.querySelector('label[for="'+el.id+'"]'); if (l && l.innerText.trim()) return l.innerText.trim(); }
        const pl = el.closest('label');
        if (pl && pl.innerText.trim()) return pl.innerText.trim();
        return '';
    }

    // 精确匹配优先: aria-label > title > 表单标签 > placeholder > innerText
    for (const el of all) {
        if (el.getAttribute('aria-label') === target) return el;
    }
    for (const el of all) {
        if (el.getAttribute('title') === target) return el;
    }
    // 表单标签精确匹配: text_locator="Name"定位到<label>Name</label>对应的input
    for (const el of all) {
        const lbl = getFormItemLabel(el);
        if (lbl === target) return el;
    }
    for (const el of all) {
        if (el.getAttribute('placeholder') === target) return el;
    }
    for (const el of all) {
        const innerText = (el.innerText || '').trim().replace(/\\s+/g, ' ');
        if (innerText === target) return el;
    }

    // 子串匹配兜底: 表单标签包含target > innerText包含target
    for (const el of all) {
        const lbl = getFormItemLabel(el);
        if (lbl && lbl.indexOf(target) !== -1) return el;
    }
    for (const el of all) {
        const innerText = (el.innerText || '').trim();
        if (innerText && innerText.indexOf(target) !== -1) return el;
    }

    return null;
}"""

CHECK_SPA_CONTENT_READY_FUNC = """() => {
    // SPA框架内容容器: 按框架优先级排列,VitePress优先(会话e5cce96a根因:
    // VitePress在Vue挂载前<div id="app">为空,body.innerText也为空,
    // 旧版仅检测body.innerText导致_wait_for_content_ready提前超时返回)
    const CONTAINERS = ['.vp-doc', '.VPContent', '[data-v-app]', '#app'];
    for (const sel of CONTAINERS) {
        const el = document.querySelector(sel);
        if (el) {
            const text = (el.innerText || '').trim();
            if (text.length > 20) {
                return {ready: true, source: sel, length: text.length};
            }
        }
    }
    // 通用回退: body.innerText长度检测(覆盖非SPA页面与已完全加载页面)
    const bodyText = (document.body && document.body.innerText || '').trim();
    return {ready: bodyText.length > 50, source: 'body', length: bodyText.length};
}"""

EXTRACT_SPA_CONTENT_FUNC = """() => {
    // SPA框架内容容器直取: 作为GET_VISIBLE_CONTENT_FUNC返回空时的回退。
    // VitePress文档内容位于.vp-doc内,DOM树遍历可能因水合期/Shadow DOM/CSS布局
    // 误判返回空,但容器innerText可直接获取文本(会话e5cce96a: 4次view返回空根因)。
    const CONTAINERS = ['.vp-doc', '.VPContent', '[data-v-app]', '#app'];
    for (const sel of CONTAINERS) {
        const el = document.querySelector(sel);
        if (el) {
            const text = (el.innerText || '').trim();
            if (text) return text;
        }
    }
    return (document.body && document.body.innerText || '').trim();
}"""

EXTRACT_DOC_CONTENT_FUNC = """() => {
    // 文档内容容器提取(视口感知): 解决DOM树遍历对文档型SPA提取全body文本
    // (含导航/侧边栏/页脚~50KB),截断后35%保底预算(~4KB)只够导航文本,
    // LLM看不到文档正文根因(会话4f441827: 51次操作,LLM被迫curl+shell_execute绕过浏览器)。
    //
    // 容器优先级(从精确到宽泛): 覆盖VitePress标准(.vp-doc)、Element Plus自定义主题(main)、
    // 通用文档语义(article/[role=main])。会话6794ac3c根因: Element Plus使用自定义VitePress主题,
    // 无.vp-doc容器,文档正文位于<main>内(5595字符,不含侧边栏),旧版仅检测.vp-doc导致漏判。
    //
    // 策略:
    // 1. 从文档容器提取文本块(跳过导航/侧边栏/页脚,只保留正文)
    // 2. 按视口位置分窗: 找到当前滚动位置对应的块,从其前2个块开始提取
    // 3. 标题添加Markdown级别前缀(## / ###),帮助LLM理解文档结构
    // 4. 去重(父子元素文本重叠) + 字符上限(8000,留余量给interactive_elements)
    const CONTAINERS = ['.vp-doc', '.VPContent', 'main', 'article', '[role="main"]'];
    let container = null;
    for (const sel of CONTAINERS) {
        const el = document.querySelector(sel);
        if (el && (el.innerText || '').trim().length > 20) { container = el; break; }
    }
    if (!container) container = document.body;
    if (!container) return '';

    const scrollY = window.scrollY;

    // 收集有意义的文本块(标题/段落/列表项/代码块/表格单元格)
    // 仅收集叶子级文本元素,避免父子元素文本重叠
    const BLOCK_SELECTOR = 'h1,h2,h3,h4,h5,h6,p,li,pre,blockquote,td,th';
    const blocks = [];
    try {
        const elements = container.querySelectorAll(BLOCK_SELECTOR);
        for (const el of elements) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            const raw = (el.innerText || '').trim();
            if (!raw || raw.length < 3) continue;
            // 标题添加Markdown级别前缀,帮助LLM识别文档结构
            const tag = el.tagName.toLowerCase();
            const prefix = ({h1:'# ',h2:'## ',h3:'### ',h4:'#### ',h5:'##### ',h6:'###### '})[tag] || '';
            // 单块上限1000字符,防止巨型代码块占满预算
            const text = prefix + (raw.length > 1000 ? raw.substring(0, 997) + '...' : raw);
            blocks.push({ text: text, top: rect.top + scrollY });
        }
    } catch(e) {
        return (container.innerText || '').trim().substring(0, 8000);
    }

    if (blocks.length === 0) {
        return (container.innerText || '').trim().substring(0, 8000);
    }

    // 按垂直位置排序(保持阅读顺序)
    blocks.sort((a, b) => a.top - b.top);

    // 去重: 跳过完全相同的文本(处理父子元素文本重叠)
    const seen = new Set();
    const unique = [];
    for (const b of blocks) {
        if (seen.has(b.text)) continue;
        seen.add(b.text);
        unique.push(b);
    }

    // 定位视口顶部对应的块索引(第一个top >= scrollY-50的块)
    let viewportIdx = 0;
    for (let i = 0; i < unique.length; i++) {
        if (unique[i].top >= scrollY - 50) { viewportIdx = i; break; }
        viewportIdx = i;
    }

    // 视口前保留2个块作为上下文(让LLM知道当前在哪个章节)
    const startIdx = Math.max(0, viewportIdx - 2);

    // 构建内容: 从视口前2个块开始,按阅读顺序提取直到字符上限
    const MAX_CHARS = 8000;
    const parts = [];
    let totalLen = 0;
    for (let i = startIdx; i < unique.length; i++) {
        if (totalLen + unique[i].text.length + 1 > MAX_CHARS) {
            parts.push('...(truncated)');
            break;
        }
        parts.push(unique[i].text);
        totalLen += unique[i].text.length + 1;
    }

    // 视口位置靠后导致前文被截断时,添加提示
    if (startIdx > 0) {
        parts.unshift('...(前文已省略,当前视口内容如下)');
    }

    return parts.join('\\n');
}"""

DETECT_DOC_CONTAINER_FUNC = """() => {
    // 文档内容容器检测: 返回匹配的容器选择器(优先级从精确到宽泛),用于_extract_content路由决策。
    // 命中时走视口感知提取(EXTRACT_DOC_CONTENT_FUNC),未命中时走DOM树结构化遍历,
    // 避免对复杂Web App误用块提取丢失交互语义(会话4f441827架构决策)。
    //
    // 容器优先级: .vp-doc/.VPContent(VitePress标准) → main(HTML5语义,Element Plus自定义主题)
    // → article(博客/文档) → [role="main"](ARIA语义)。
    // 会话6794ac3c根因: Element Plus使用自定义VitePress主题,无.vp-doc容器,文档正文位于<main>,
    // 旧版仅检测.vp-doc/.VPContent导致漏判,content字段全空(5次browser_view均无页面文本)。
    const CONTAINERS = ['.vp-doc', '.VPContent', 'main', 'article', '[role="main"]'];
    for (const sel of CONTAINERS) {
        const el = document.querySelector(sel);
        if (el && (el.innerText || '').trim().length > 20) return sel;
    }
    return null;
}"""
