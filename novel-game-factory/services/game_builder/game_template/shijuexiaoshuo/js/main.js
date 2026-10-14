'use strict';
/* global Monogatari */
/* global monogatari */

/**
 * =============================================================================
 * This is the file where you should put all your custom JavaScript code,
 * depending on what you want to do, there are 3 different places in this file
 * where you can add code.
 *
 * 1. Outside the $_ready function: At this point, the page may not be fully
 *    loaded yet, however you can interact with Monogatari to register new
 *    actions, components, labels, characters, etc.
 *
 * 2. Inside the $_ready function: At this point, the page has been loaded, and
 *    you can now interact with the HTML elements on it.
 *
 * 3. Inside the init function: At this point, Monogatari has been initialized,
 *    the event listeners for its inner workings have been registered, assets
 *    have been preloaded (if enabled) and your game is ready to be played.
 *
 * You should always keep the $_ready function as the last thing on this file.
 * =============================================================================
 **/

const { $_ready, $_ } = Monogatari;

// 1. Outside the $_ready function:

$_ready (() => {
	// 2. Inside the $_ready function:

	// ==================== 存档数量限制 ====================
	// 拦截 localStorage 来限制存档数量（最多3个）
	// 这是一个底层拦截，作为备用保护机制
	const originalSetItem = localStorage.setItem.bind(localStorage);
	localStorage.setItem = function(key, value) {
		// 检查是否是保存游戏存档（包含 _Save_ 或 _AutoSave_）
		if (key.includes('_Save_') || key.includes('_AutoSave_')) {
			// 获取所有存档
			const saveKeys = Object.keys(localStorage).filter(k => k.includes('_Save_') || k.includes('_AutoSave_'));

			// 如果已有3个存档且当前key不在已有存档中，则阻止保存
			if (saveKeys.length >= 3 && !saveKeys.includes(key)) {
				const lang = String(window._lang || new URLSearchParams(window.location.search).get('lang') || '');
				alert(lang === 'ja-JP'
					? 'セーブスロットは3件までです。既存のセーブを削除してください。'
					: 'Save slots are full (max 3). Please delete an existing save first.');
				throw new Error('Save limit reached: Maximum 3 slots allowed.');
			}
		}
		return originalSetItem(key, value);
	};

	monogatari.init ('#monogatari').then (() => {
		// 3. Inside the init function:
		console.log('Visual novel started');
	});
});
