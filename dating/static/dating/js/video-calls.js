(() => {
    const config = document.getElementById('call-config');
    if (!config) return;

    // Elements - Incoming Call Sheet
    const incomingSheet = document.getElementById('incoming-call-sheet');
    const incomingName = document.getElementById('incoming-call-title');
    const incomingPhoto = document.getElementById('incoming-caller-photo');
    const fallbackPhoto = incomingPhoto ? incomingPhoto.src : '';

    // Elements - Video Room
    const roomOverlay = document.getElementById('video-call-room');
    const remoteVideo = document.getElementById('remote-call-video');
    const localVideo = document.getElementById('local-call-video');
    const feedback = document.getElementById('call-feedback');
    const callTimerDisplay = document.getElementById('call-timer-display');
    const callBillingBadge = document.getElementById('call-billing-badge');
    const callHudCoinsWrapper = document.getElementById('call-hud-coins-wrapper');
    const callHudCoins = document.getElementById('call-hud-coins');
    const callHudDiamondsWrapper = document.getElementById('call-hud-diamonds-wrapper');
    const callHudDiamonds = document.getElementById('call-hud-diamonds');
    const callQuickAddCoins = document.getElementById('call-quick-add-coins');
    const callLowCoinsBanner = document.getElementById('call-low-coins-banner');
    const callBannerTopupBtn = document.getElementById('call-banner-topup-btn');
    const callGiftOverlay = document.getElementById('call-gift-overlay');
    const callGiftDrawer = document.getElementById('call-gift-drawer');
    const toggleGiftsBtn = document.getElementById('toggle-gifts');

    // Elements - Navbar & Coin Store Modal
    const navCoinBalance = document.getElementById('nav-coin-balance');
    const navDiamondPill = document.getElementById('nav-diamond-pill');
    const navDiamondBalance = document.getElementById('nav-diamond-balance');
    const openCoinStoreBtn = document.getElementById('open-coin-store-btn');
    const coinStoreModal = document.getElementById('coin-store-modal');
    const closeCoinStoreBtn = document.getElementById('close-coin-store-btn');
    const storeModalCoins = document.getElementById('store-modal-coins');
    const storeModalDiamonds = document.getElementById('store-modal-diamonds');
    const coinPackagesList = document.getElementById('coin-packages-list');
    const storePaymentStatus = document.getElementById('store-payment-status');

    // State Variables
    const apiPrefix = config.dataset.callApiPrefix;
    let activeCall = null;
    let peer = null;
    let localStream = null;
    let lastSignalId = 0;
    let signalingTimer = null;
    let statusTimer = null;
    let heartbeatTimer = null;
    let callTimerInterval = null;
    let callDurationSeconds = 0;
    let pendingCandidates = [];
    let cameraFacing = 'user';
    let isCaller = false;
    let incomingRoomId = null;

    // Wallet State
    let userCoins = 0;
    let userDiamonds = 0;
    let isSexCallPremium = false;
    let coinPackages = [];
    let paymentKeys = { paystack: '', flutterwave: '', paystackEnabled: false, flutterwaveEnabled: false };

    const giftIcons = {
        rose: '🌹',
        kiss: '💋',
        champagne: '🥂',
        crown: '👑',
        car: '🏎️',
        ring: '💍',
        yacht: '🛥️',
    };
    const giftNames = {
        rose: 'Rose',
        kiss: 'Kiss',
        champagne: 'Champagne',
        crown: 'Crown',
        car: 'Supercar',
        ring: 'Diamond Ring',
        yacht: 'Luxury Yacht',
    };

    // --- Web Audio Ringtone Synthesizer ---
    let audioCtx = null;
    let ringInterval = null;
    let ringOscillators = [];

    function getAudioContext() {
        if (!audioCtx) {
            const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
            if (AudioCtxClass) audioCtx = new AudioCtxClass();
        }
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(() => {});
        }
        return audioCtx;
    }

    function stopRingtone() {
        if (ringInterval) {
            clearInterval(ringInterval);
            ringInterval = null;
        }
        ringOscillators.forEach(osc => {
            try { osc.stop(); osc.disconnect(); } catch (e) {}
        });
        ringOscillators = [];
    }

    function playOutgoingRing() {
        stopRingtone();
        const ctx = getAudioContext();
        if (!ctx) return;

        function beep() {
            if (!activeCall || !isCaller || activeCall.status !== 'ringing') {
                stopRingtone();
                return;
            }
            try {
                const now = ctx.currentTime;
                const osc1 = ctx.createOscillator();
                const osc2 = ctx.createOscillator();
                const gain = ctx.createGain();
                osc1.type = 'sine'; osc1.frequency.setValueAtTime(440, now);
                osc2.type = 'sine'; osc2.frequency.setValueAtTime(480, now);
                gain.gain.setValueAtTime(0.06, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 1.2);

                osc1.connect(gain); osc2.connect(gain);
                gain.connect(ctx.destination);
                osc1.start(now); osc2.start(now);
                osc1.stop(now + 1.2); osc2.stop(now + 1.2);
                ringOscillators = [osc1, osc2];
            } catch (e) {}
        }
        beep();
        ringInterval = setInterval(beep, 3000);
    }

    function playIncomingRing() {
        stopRingtone();
        const ctx = getAudioContext();
        if (!ctx) return;

        function melody() {
            if (!incomingRoomId) {
                stopRingtone();
                return;
            }
            try {
                const now = ctx.currentTime;
                const notes = [523.25, 659.25, 783.99, 1046.5];
                notes.forEach((freq, idx) => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    const start = now + idx * 0.16;
                    osc.type = 'triangle';
                    osc.frequency.setValueAtTime(freq, start);
                    gain.gain.setValueAtTime(0.10, start);
                    gain.gain.exponentialRampToValueAtTime(0.001, start + 0.15);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(start);
                    osc.stop(start + 0.15);
                    ringOscillators.push(osc);
                });
            } catch (e) {}
        }
        melody();
        ringInterval = setInterval(melody, 2200);
    }

    function csrfToken() {
        const token = document.cookie.split('; ').find(item => item.startsWith('csrftoken='));
        return token ? decodeURIComponent(token.split('=').slice(1).join('=')) : '';
    }

    async function api(url, options = {}) {
        const headers = {'Accept': 'application/json', ...(options.headers || {})};
        if (options.method && options.method !== 'GET') {
            headers['Content-Type'] = 'application/json';
            headers['X-CSRFToken'] = csrfToken();
        }
        const response = await fetch(url, {...options, headers, credentials: 'same-origin'});
        const payload = await response.json();
        if (!response.ok) {
            const error = new Error(payload.message || 'The call request could not be completed.');
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function toast(message, isCoinStorePrompt = false) {
        const notice = document.createElement('div');
        notice.className = 'fixed left-1/2 top-5 z-[160] w-[min(92vw,28rem)] -translate-x-1/2 rounded-2xl border border-white/70 bg-slate-900 px-4 py-3 text-center text-sm font-semibold text-white shadow-2xl flex items-center justify-between gap-3';
        
        const textSpan = document.createElement('span');
        textSpan.textContent = message;
        notice.appendChild(textSpan);

        if (isCoinStorePrompt) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'whitespace-nowrap rounded-xl bg-gradient-to-r from-amber-500 to-yellow-300 px-3 py-1 text-xs font-black text-slate-950 shadow';
            btn.textContent = 'Get Coins';
            btn.onclick = () => {
                notice.remove();
                openCoinStore();
            };
            notice.appendChild(btn);
        }
        document.body.appendChild(notice);
        window.setTimeout(() => { if (notice.parentNode) notice.remove(); }, 5000);
    }

    // --- Wallet Management ---
    async function fetchWallet() {
        if (!config.dataset.walletUrl) return;
        try {
            const data = await api(config.dataset.walletUrl);
            if (data.status === 'success') {
                userCoins = data.coin_balance || 0;
                userDiamonds = data.earned_diamonds || 0;
                isSexCallPremium = !!data.is_sex_call_premium;
                updateWalletUI();
            }
        } catch (err) {
            console.warn('Could not load wallet data.', err);
        }
    }

    function updateWalletUI() {
        if (navCoinBalance) navCoinBalance.textContent = userCoins;
        if (storeModalCoins) storeModalCoins.textContent = userCoins;
        if (callHudCoins) callHudCoins.textContent = userCoins;

        if (navDiamondBalance) navDiamondBalance.textContent = userDiamonds;
        if (storeModalDiamonds) storeModalDiamonds.textContent = userDiamonds;
        if (callHudDiamonds) callHudDiamonds.textContent = userDiamonds;

        if (navDiamondPill) {
            if (userDiamonds > 0) {
                navDiamondPill.classList.remove('hidden');
                navDiamondPill.classList.add('flex');
            } else {
                navDiamondPill.classList.add('hidden');
                navDiamondPill.classList.remove('flex');
            }
        }
        if (callHudDiamondsWrapper) {
            if (userDiamonds > 0 || !isCaller) {
                callHudDiamondsWrapper.classList.remove('hidden');
                callHudDiamondsWrapper.classList.add('flex');
            } else {
                callHudDiamondsWrapper.classList.add('hidden');
                callHudDiamondsWrapper.classList.remove('flex');
            }
        }
    }

    // --- Coin Packages & Payment Modal ---
    async function fetchCoinPackages() {
        if (!config.dataset.packagesUrl) return;
        try {
            const response = await api(config.dataset.packagesUrl);
            coinPackages = response.packages || [];
            paymentKeys.paystack = response.paystack_public_key || '';
            paymentKeys.flutterwave = response.flutterwave_public_key || '';
            paymentKeys.paystackEnabled = !!response.paystack_enabled;
            paymentKeys.flutterwaveEnabled = !!response.flutterwave_enabled;
            renderCoinPackages();
        } catch (err) {
            console.warn('Could not load coin packages', err);
        }
    }

    function renderCoinPackages() {
        if (!coinPackagesList) return;
        if (!coinPackages.length) {
            coinPackagesList.innerHTML = '<div class="col-span-full py-6 text-center text-sm text-slate-400">No coin packages found.</div>';
            return;
        }

        coinPackagesList.innerHTML = coinPackages.map(pkg => {
            const approxMins = (pkg.total_coins / 20).toFixed(0);
            const badgeHtml = pkg.badge
                ? `<span class="absolute -top-2.5 right-3 rounded-full bg-gradient-to-r from-pink-500 to-rose-500 px-2.5 py-0.5 text-[10px] font-black uppercase tracking-wider text-white shadow">${pkg.badge}</span>`
                : (pkg.is_popular ? `<span class="absolute -top-2.5 right-3 rounded-full bg-gradient-to-r from-amber-500 to-orange-500 px-2.5 py-0.5 text-[10px] font-black uppercase tracking-wider text-slate-950 shadow">Most Popular</span>` : '');

            return `
            <div class="relative flex flex-col justify-between rounded-2xl border ${pkg.is_popular ? 'border-amber-400 bg-amber-50/50 ring-2 ring-amber-300/60' : 'border-slate-200 bg-white'} p-4 shadow-sm hover:shadow-md transition">
                ${badgeHtml}
                <div>
                    <div class="flex items-center gap-1.5 text-lg font-black text-slate-900">
                        <span class="text-xl">🪙</span>
                        <span>${pkg.total_coins} Coins</span>
                    </div>
                    ${pkg.bonus_coins > 0 ? `<p class="text-[11px] font-bold text-emerald-600">+${pkg.bonus_coins} bonus coins included!</p>` : ''}
                    <p class="mt-1 text-xs text-slate-500">~${approxMins} minutes video call time</p>
                    <p class="mt-2 text-xl font-black text-slate-950">₦${Number(pkg.price).toLocaleString()}</p>
                </div>
                <div class="mt-3 flex flex-col gap-1.5">
                    ${paymentKeys.paystackEnabled ? `
                    <button type="button" class="buy-pkg-paystack w-full rounded-xl bg-[#09A5DB] px-3 py-2 text-xs font-black text-white shadow transition hover:brightness-105 active:scale-95" data-pkg-id="${pkg.id}" data-price="${pkg.price}" data-name="${pkg.name}" data-coins="${pkg.total_coins}">
                        Pay with Paystack
                    </button>` : ''}
                    ${paymentKeys.flutterwaveEnabled ? `
                    <button type="button" class="buy-pkg-flutterwave w-full rounded-xl bg-[#F5A623] px-3 py-2 text-xs font-black text-white shadow transition hover:brightness-105 active:scale-95" data-pkg-id="${pkg.id}" data-price="${pkg.price}" data-name="${pkg.name}" data-coins="${pkg.total_coins}">
                        Pay with Flutterwave
                    </button>` : ''}
                    ${!paymentKeys.paystackEnabled && !paymentKeys.flutterwaveEnabled ? `
                    <button disabled class="w-full rounded-xl bg-slate-200 px-3 py-2 text-xs font-bold text-slate-400 cursor-not-allowed">
                        Setup Mode
                    </button>` : ''}
                </div>
            </div>`;
        }).join('');

        coinPackagesList.querySelectorAll('.buy-pkg-paystack').forEach(btn => {
            btn.onclick = () => initiateCoinPurchase(btn.dataset.pkgId, Number(btn.dataset.price), 'paystack', btn.dataset.name);
        });
        coinPackagesList.querySelectorAll('.buy-pkg-flutterwave').forEach(btn => {
            btn.onclick = () => initiateCoinPurchase(btn.dataset.pkgId, Number(btn.dataset.price), 'flutterwave', btn.dataset.name);
        });
    }

    function openCoinStore() {
        if (!coinStoreModal) return;
        coinStoreModal.classList.remove('hidden');
        coinStoreModal.classList.add('flex');
        if (storePaymentStatus) storePaymentStatus.textContent = '';
        if (!coinPackages.length) fetchCoinPackages();
        fetchWallet();
    }

    function closeCoinStore() {
        if (!coinStoreModal) return;
        coinStoreModal.classList.add('hidden');
        coinStoreModal.classList.remove('flex');
    }

    function initiateCoinPurchase(packageId, price, provider, packageName) {
        const userEmail = config.dataset.userEmail;
        if (!userEmail) {
            toast('A verified email address is required to complete payments.');
            return;
        }

        if (storePaymentStatus) {
            storePaymentStatus.textContent = `Opening secure ${provider} checkout…`;
            storePaymentStatus.className = 'mt-3 min-h-5 text-center text-xs font-bold text-slate-600 animate-pulse';
        }

        if (provider === 'paystack') {
            if (typeof PaystackPop === 'undefined') {
                toast('Paystack payment gateway is still loading. Please try again in a moment.');
                return;
            }
            const handler = PaystackPop.setup({
                key: paymentKeys.paystack,
                email: userEmail,
                amount: Math.round(price * 100),
                currency: 'NGN',
                callback: response => verifyCoinPayment(packageId, 'paystack', response.reference),
                onClose: () => {
                    if (storePaymentStatus) storePaymentStatus.textContent = 'Checkout was cancelled.';
                },
            });
            handler.openIframe();
        } else if (provider === 'flutterwave') {
            if (typeof FlutterwaveCheckout === 'undefined') {
                toast('Flutterwave checkout gateway is still loading. Please try again in a moment.');
                return;
            }
            const txRef = `LOVENY_COINS_${packageId}_${Date.now()}`;
            FlutterwaveCheckout({
                public_key: paymentKeys.flutterwave,
                tx_ref: txRef,
                amount: price,
                currency: 'NGN',
                payment_options: 'card, mobilemoney, ussd',
                customer: { email: userEmail },
                customizations: {
                    title: `LOVENY Coin Store`,
                    description: `${packageName} coins`,
                },
                callback: data => verifyCoinPayment(packageId, 'flutterwave', data.tx_ref, String(data.transaction_id)),
                onclose: () => {
                    if (storePaymentStatus) storePaymentStatus.textContent = 'Checkout was closed.';
                },
            });
        }
    }

    async function verifyCoinPayment(packageId, provider, reference, transactionId = '') {
        if (storePaymentStatus) {
            storePaymentStatus.textContent = 'Verifying payment with server…';
            storePaymentStatus.className = 'mt-3 min-h-5 text-center text-xs font-bold text-amber-600 animate-pulse';
        }
        try {
            const result = await api(config.dataset.verifyUrl, {
                method: 'POST',
                body: JSON.stringify({
                    product: 'COINS',
                    package_id: packageId,
                    provider,
                    reference,
                    transaction_id: transactionId,
                }),
            });
            userCoins = result.coin_balance;
            updateWalletUI();
            if (storePaymentStatus) {
                storePaymentStatus.textContent = `🎉 Payment verified! +${result.added_coins || ''} Coins added!`;
                storePaymentStatus.className = 'mt-3 min-h-5 text-center text-xs font-bold text-emerald-600';
            }
            toast(`🎉 Added +${result.added_coins || ''} coins to your balance!`);
            // Dismiss low coin warning if active in room
            if (callLowCoinsBanner) {
                callLowCoinsBanner.classList.add('hidden');
                callLowCoinsBanner.classList.remove('flex');
            }
            window.setTimeout(() => {
                closeCoinStore();
                if (storePaymentStatus) storePaymentStatus.textContent = '';
            }, 1400);
        } catch (err) {
            if (storePaymentStatus) {
                storePaymentStatus.textContent = err.message || 'Payment verification failed.';
                storePaymentStatus.className = 'mt-3 min-h-5 text-center text-xs font-bold text-rose-600';
            }
        }
    }

    // --- Web Audio Synthesizer Chime ---
    function playCelebrationSound() {
        try {
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            if (!AudioCtx) return;
            const ctx = new AudioCtx();
            const now = ctx.currentTime;
            const notes = [523.25, 659.25, 783.99, 1046.50];
            notes.forEach((freq, idx) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(freq, now + idx * 0.08);
                gain.gain.setValueAtTime(0.12, now + idx * 0.08);
                gain.gain.exponentialRampToValueAtTime(0.001, now + idx * 0.08 + 0.35);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(now + idx * 0.08);
                osc.stop(now + idx * 0.08 + 0.35);
            });
        } catch (_) {}
    }

    // --- In-Call Floating Celebratory Gift Animation ---
    function triggerGiftAnimation(gift) {
        if (!callGiftOverlay) return;
        const emoji = giftIcons[gift.gift_type] || '🎁';
        const name = giftNames[gift.gift_type] || 'Gift';
        const sender = gift.sender_name || (gift.is_self ? 'You' : 'Someone');

        playCelebrationSound();

        const container = document.createElement('div');
        container.className = 'absolute flex flex-col items-center pointer-events-none transition-all duration-700 ease-out';
        container.style.bottom = '35%';
        container.style.opacity = '0';
        container.style.transform = 'translateY(50px) scale(0.6)';

        const iconEl = document.createElement('div');
        iconEl.className = 'text-7xl sm:text-8xl drop-shadow-[0_12px_24px_rgba(0,0,0,0.6)] animate-bounce';
        iconEl.textContent = emoji;

        const bannerEl = document.createElement('div');
        bannerEl.className = 'mt-3 rounded-full border border-pink-300/40 bg-gradient-to-r from-pink-600 via-rose-500 to-amber-500 px-5 py-2 text-center text-sm font-black text-white shadow-2xl backdrop-blur-md';
        bannerEl.textContent = `${sender} sent ${emoji} ${name}!`;
        if (gift.diamond_award && !gift.is_self) {
            bannerEl.textContent += ` (+${gift.diamond_award} 💎)`;
        }

        container.appendChild(iconEl);
        container.appendChild(bannerEl);
        callGiftOverlay.appendChild(container);

        requestAnimationFrame(() => {
            container.style.opacity = '1';
            container.style.transform = 'translateY(0) scale(1.15)';
        });

        window.setTimeout(() => {
            container.style.opacity = '0';
            container.style.transform = 'translateY(-140px) scale(0.85)';
            window.setTimeout(() => container.remove(), 750);
        }, 2600);
    }

    async function sendGift(giftType, cost) {
        if (!activeCall) return;
        if (userCoins < cost) {
            toast(`Not enough coins for this gift (${cost} 🪙 required). Top up your wallet!`, true);
            openCoinStore();
            return;
        }
        try {
            const response = await api(`${apiPrefix}${activeCall.room_id}/gift/`, {
                method: 'POST',
                body: JSON.stringify({gift_type: giftType}),
            });
            userCoins = response.caller_coins;
            updateWalletUI();
            if (callGiftDrawer) callGiftDrawer.classList.add('hidden');
            triggerGiftAnimation({
                gift_type: giftType,
                sender_name: 'You',
                is_self: true,
                diamond_award: response.diamond_award,
            });
        } catch (err) {
            toast(err.message || 'Could not send gift.');
        }
    }

    // --- Call Telemetry, Timers & Heartbeats ---
    function startCallTimer() {
        stopRingtone();
        stopCallTimer();
        callDurationSeconds = 0;
        updateCallTimerDisplay();
        callTimerInterval = window.setInterval(() => {
            callDurationSeconds++;
            updateCallTimerDisplay();
        }, 1000);

        heartbeatTimer = window.setInterval(sendHeartbeat, 10000);
    }

    function stopCallTimer() {
        if (callTimerInterval) window.clearInterval(callTimerInterval);
        if (heartbeatTimer) window.clearInterval(heartbeatTimer);
        callTimerInterval = null;
        heartbeatTimer = null;
        callDurationSeconds = 0;
        if (callLowCoinsBanner) {
            callLowCoinsBanner.classList.add('hidden');
            callLowCoinsBanner.classList.remove('flex');
        }
        if (callGiftDrawer) callGiftDrawer.classList.add('hidden');
    }

    function updateCallTimerDisplay() {
        if (!callTimerDisplay) return;
        const mins = Math.floor(callDurationSeconds / 60);
        const secs = callDurationSeconds % 60;
        callTimerDisplay.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;

        if (callDurationSeconds < 20) {
            if (callBillingBadge) {
                callBillingBadge.textContent = '🛡️ First 20s Free';
                callBillingBadge.className = 'rounded-full bg-emerald-500/90 px-2.5 py-1 text-[11px] font-bold text-white shadow backdrop-blur-md';
            }
        } else {
            if (callBillingBadge) {
                callBillingBadge.textContent = isSexCallPremium ? '👑 Pass Active' : '🪙 20/min';
                callBillingBadge.className = 'rounded-full bg-black/55 px-2.5 py-1 text-[11px] font-medium text-white/90 backdrop-blur-md';
            }
        }

        // Low coin estimate for caller
        if (isCaller && !isSexCallPremium) {
            const billableElapsed = Math.max(0, callDurationSeconds - 20);
            const billableMinutes = Math.ceil(billableElapsed / 60);
            const estimatedSpent = billableMinutes * 20;
            const remainingCoins = Math.max(0, userCoins - estimatedSpent);
            const remainingSeconds = (remainingCoins / 20) * 60;

            if (remainingSeconds <= 30 && callLowCoinsBanner) {
                callLowCoinsBanner.classList.remove('hidden');
                callLowCoinsBanner.classList.add('flex');
            } else if (callLowCoinsBanner) {
                callLowCoinsBanner.classList.add('hidden');
                callLowCoinsBanner.classList.remove('flex');
            }
        }
    }

    async function sendHeartbeat() {
        if (!activeCall) return;
        try {
            const response = await api(`${apiPrefix}${activeCall.room_id}/heartbeat/`, {
                method: 'POST',
                body: JSON.stringify({duration_seconds: callDurationSeconds}),
            });
            if (response.call?.status === 'ended') {
                closeRoom();
                toast(response.reason === 'coins_exhausted'
                    ? 'Call ended: Out of coins. Top up your wallet to continue!'
                    : 'The call has ended.', response.reason === 'coins_exhausted');
                fetchWallet();
                return;
            }
            if (response.call) {
                if (isCaller && response.call.caller_coins !== undefined) {
                    userCoins = response.call.caller_coins;
                    updateWalletUI();
                } else if (!isCaller && response.call.receiver_diamonds !== undefined) {
                    userDiamonds = response.call.receiver_diamonds;
                    updateWalletUI();
                }
                if (response.call.remaining_seconds !== null && response.call.remaining_seconds <= 30) {
                    if (callLowCoinsBanner && isCaller) {
                        callLowCoinsBanner.classList.remove('hidden');
                        callLowCoinsBanner.classList.add('flex');
                    }
                }
            }
        } catch (err) {
            console.warn('Heartbeat error', err);
        }
    }

    // --- WebRTC Core Functions ---
    function showIncoming(call) {
        if (activeCall || incomingRoomId === call.room_id) return;
        incomingRoomId = call.room_id;
        if (incomingName) incomingName.textContent = `${call.caller_name || 'Someone'} is calling`;
        if (incomingPhoto) {
            incomingPhoto.src = call.caller_photo || fallbackPhoto;
            incomingPhoto.onerror = () => { incomingPhoto.onerror = null; incomingPhoto.src = fallbackPhoto; };
        }
        if (incomingSheet) {
            incomingSheet.classList.remove('hidden');
            incomingSheet.classList.add('flex');
            playIncomingRing();
        }
    }

    function hideIncoming() {
        incomingRoomId = null;
        stopRingtone();
        if (incomingSheet) {
            incomingSheet.classList.add('hidden');
            incomingSheet.classList.remove('flex');
        }
    }

    function showRoom(message = 'Connecting…') {
        if (roomOverlay) roomOverlay.classList.remove('hidden');
        if (feedback) feedback.textContent = message;
        updateWalletUI();
        if (callTimerDisplay) callTimerDisplay.textContent = '00:00';
    }

    function stopPolling() {
        window.clearInterval(signalingTimer);
        window.clearInterval(statusTimer);
        signalingTimer = null;
        statusTimer = null;
        stopCallTimer();
    }

    function closeRoom() {
        stopPolling();
        stopRingtone();
        if (peer) {
            peer.onicecandidate = null;
            peer.ontrack = null;
            peer.close();
            peer = null;
        }
        if (localStream) {
            localStream.getTracks().forEach(track => track.stop());
            localStream = null;
        }
        if (remoteVideo) remoteVideo.srcObject = null;
        if (localVideo) localVideo.srcObject = null;
        if (roomOverlay) roomOverlay.classList.add('hidden');
        activeCall = null;
        pendingCandidates = [];
        lastSignalId = 0;
        fetchWallet();
    }

    async function sendSignal(type, payload) {
        if (!activeCall) return;
        try {
            await api(`${apiPrefix}${activeCall.room_id}/signals/send/`, {
                method: 'POST',
                body: JSON.stringify({type, payload}),
            });
        } catch (error) {
            if (feedback) feedback.textContent = error.message;
        }
    }

    async function flushCandidates() {
        if (!peer || !peer.remoteDescription) return;
        const queued = pendingCandidates;
        pendingCandidates = [];
        for (const candidate of queued) {
            try {
                await peer.addIceCandidate(candidate);
            } catch (error) {
                console.warn('Could not apply remote ICE candidate.', error);
            }
        }
    }

    async function consumeSignal(signal) {
        if (!activeCall) return;
        // In-call Gift Signal handling
        if (signal.type === 'gift') {
            if (!isCaller && signal.payload.diamond_award) {
                userDiamonds += signal.payload.diamond_award;
                updateWalletUI();
            }
            triggerGiftAnimation(signal.payload);
            return;
        }

        if (!peer) return;
        try {
            if (signal.type === 'offer' && !isCaller && !peer.remoteDescription) {
                await peer.setRemoteDescription(new RTCSessionDescription(signal.payload));
                await flushCandidates();
                const answer = await peer.createAnswer();
                await peer.setLocalDescription(answer);
                await sendSignal('answer', peer.localDescription.toJSON());
                if (feedback) feedback.textContent = 'Connecting with partner…';
            } else if (signal.type === 'answer' && isCaller && !peer.remoteDescription) {
                await peer.setRemoteDescription(new RTCSessionDescription(signal.payload));
                await flushCandidates();
                if (feedback) feedback.textContent = 'Call connected';
                startCallTimer();
            } else if (signal.type === 'candidate') {
                const candidate = new RTCIceCandidate(signal.payload);
                if (peer.remoteDescription) await peer.addIceCandidate(candidate);
                else pendingCandidates.push(candidate);
            }
        } catch (error) {
            if (feedback) feedback.textContent = 'Could not establish the video connection.';
            console.error('WebRTC signaling failed.', error);
        }
    }

    async function pollSignals() {
        if (!activeCall || document.visibilityState !== 'visible') return;
        try {
            const response = await api(`${apiPrefix}${activeCall.room_id}/signals/?after_id=${lastSignalId}`);
            for (const signal of response.signals) {
                lastSignalId = Math.max(lastSignalId, signal.id);
                await consumeSignal(signal);
            }
        } catch (error) {
            if (feedback) feedback.textContent = error.message;
        }
    }

    async function pollCallStatus() {
        if (!activeCall || document.visibilityState !== 'visible') return;
        try {
            const response = await api(`${apiPrefix}${activeCall.room_id}/`);
            if (['ended', 'declined'].includes(response.call.status)) {
                closeRoom();
                toast(response.call.status === 'declined' ? 'The call was declined.' : 'The call has ended.');
            }
        } catch (error) {
            if (feedback) feedback.textContent = error.message;
        }
    }

    function startPolling() {
        stopPolling();
        signalingTimer = window.setInterval(pollSignals, 1400);
        statusTimer = window.setInterval(pollCallStatus, 2200);
        pollSignals();
        pollCallStatus();
    }

    async function createPeer() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error('Video calling requires a supported browser and a secure connection (HTTPS or localhost).');
        }
        localStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: {facingMode: cameraFacing},
        });
        if (localVideo) localVideo.srcObject = localStream;
        peer = new RTCPeerConnection({
            iceServers: [{urls: 'stun:stun.l.google.com:19302'}],
        });
        localStream.getTracks().forEach(track => peer.addTrack(track, localStream));
        peer.onicecandidate = event => {
            if (event.candidate) sendSignal('candidate', event.candidate.toJSON());
        };
        peer.ontrack = event => {
            if (remoteVideo) {
                remoteVideo.srcObject = event.streams[0] || new MediaStream([event.track]);
                remoteVideo.play().catch(() => {});
            }
            if (feedback) feedback.textContent = 'Call connected';
            startCallTimer();
        };
        peer.onconnectionstatechange = () => {
            if (peer && peer.connectionState === 'connected') {
                if (feedback) feedback.textContent = 'Call connected';
                startCallTimer();
            }
            if (peer && ['failed', 'disconnected'].includes(peer.connectionState)) {
                if (feedback) feedback.textContent = 'Connection interrupted. Reconnecting…';
            }
        };
    }

    async function initiateCall(receiverId) {
        // Pre-check coins locally to guide user proactively
        if (userCoins < 20 && !isSexCallPremium) {
            toast('At least 20 coins (or a Sex Call Pass) is required to start a call. Opening Coin Store...', true);
            openCoinStore();
            return;
        }

        try {
            const response = await api(config.dataset.initiateUrl, {
                method: 'POST',
                body: JSON.stringify({receiver_id: receiverId}),
            });
            activeCall = response.call;
            isCaller = true;
            if (response.call.caller_coins !== undefined) {
                userCoins = response.call.caller_coins;
                updateWalletUI();
            }
            showRoom('Starting secure video…');
            playOutgoingRing();
            await createPeer();
            const offer = await peer.createOffer();
            await peer.setLocalDescription(offer);
            await sendSignal('offer', peer.localDescription.toJSON());
            startPolling();
        } catch (error) {
            closeRoom();
            const isPassOrCoinsNeeded = error.status === 403 && (
                error.payload?.message === 'sex_call_pass_required' ||
                error.payload?.message === 'insufficient_coins'
            );
            if (isPassOrCoinsNeeded) {
                toast('Sex Call requires at least 20 coins (1 min rate) or a Sex Call Pass.', true);
                openCoinStore();
            } else {
                toast(error.message || 'Could not start the video call. Check camera and microphone permissions.');
            }
        }
    }

    async function respondToCall(action) {
        const roomId = incomingRoomId;
        if (!roomId) return;
        hideIncoming();
        stopRingtone();
        if (action === 'decline') {
            try {
                await api(`${apiPrefix}${roomId}/respond/`, {
                    method: 'POST',
                    body: JSON.stringify({action}),
                });
            } catch (error) {
                toast(error.message);
            }
            return;
        }
        try {
            const response = await api(`${apiPrefix}${roomId}/respond/`, {
                method: 'POST',
                body: JSON.stringify({action}),
            });
            activeCall = response.call;
            isCaller = false;
            showRoom('Joining call…');
            await createPeer();
            startPolling();
        } catch (error) {
            closeRoom();
            try {
                await api(`${apiPrefix}${roomId}/end/`, {method: 'POST', body: '{}'});
            } catch (cleanupError) {
                console.warn('Could not end call after media setup failed.', cleanupError);
            }
            toast(error.message || 'Could not join call. Allow camera and microphone permissions to continue.');
        }
    }

    // --- UI Event Listeners ---
    document.querySelectorAll('[data-video-call-target]').forEach(button => {
        button.addEventListener('click', () => initiateCall(button.dataset.videoCallTarget));
    });

    const acceptCallBtn = document.getElementById('accept-call');
    if (acceptCallBtn) acceptCallBtn.addEventListener('click', () => respondToCall('accept'));

    const declineCallBtn = document.getElementById('decline-call');
    if (declineCallBtn) declineCallBtn.addEventListener('click', () => respondToCall('decline'));

    const endCallBtn = document.getElementById('end-call');
    if (endCallBtn) endCallBtn.addEventListener('click', async () => {
        const roomId = activeCall && activeCall.room_id;
        closeRoom();
        if (roomId) {
            try {
                const res = await api(`${apiPrefix}${roomId}/end/`, {method: 'POST', body: '{}'});
                if (res.call?.grace_period_applied) {
                    toast('Call ended: Under 20s grace period — 0 coins deducted!');
                }
            } catch (error) {
                console.warn('Call end error', error);
            }
        }
    });

    const muteCallBtn = document.getElementById('mute-call');
    if (muteCallBtn) muteCallBtn.addEventListener('click', event => {
        if (!localStream) return;
        const enabled = localStream.getAudioTracks().some(track => track.enabled);
        localStream.getAudioTracks().forEach(track => { track.enabled = !enabled; });
        event.currentTarget.textContent = enabled ? 'Unmute' : 'Mute';
        event.currentTarget.setAttribute('aria-label', enabled ? 'Unmute audio' : 'Mute audio');
    });

    const toggleCamBtn = document.getElementById('toggle-camera');
    if (toggleCamBtn) toggleCamBtn.addEventListener('click', event => {
        if (!localStream) return;
        const enabled = localStream.getVideoTracks().some(track => track.enabled);
        localStream.getVideoTracks().forEach(track => { track.enabled = !enabled; });
        event.currentTarget.textContent = enabled ? 'Cam off' : 'Cam';
        event.currentTarget.setAttribute('aria-label', enabled ? 'Turn camera on' : 'Turn camera off');
    });

    const flipCamBtn = document.getElementById('flip-camera');
    if (flipCamBtn) flipCamBtn.addEventListener('click', async () => {
        if (!peer || !localStream) return;
        cameraFacing = cameraFacing === 'user' ? 'environment' : 'user';
        try {
            const replacement = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {facingMode: {ideal: cameraFacing}},
            });
            const newTrack = replacement.getVideoTracks()[0];
            const sender = peer.getSenders().find(item => item.track && item.track.kind === 'video');
            if (sender) await sender.replaceTrack(newTrack);
            const oldTrack = localStream.getVideoTracks()[0];
            localStream.removeTrack(oldTrack);
            oldTrack.stop();
            localStream.addTrack(newTrack);
            replacement.getTracks().filter(track => track !== newTrack).forEach(track => track.stop());
            if (localVideo) localVideo.srcObject = localStream;
        } catch (error) {
            toast('Could not switch cameras. The current camera will stay on.');
            cameraFacing = cameraFacing === 'user' ? 'environment' : 'user';
        }
    });

    // In-call gift toggling
    if (toggleGiftsBtn) {
        toggleGiftsBtn.addEventListener('click', () => {
            if (!callGiftDrawer) return;
            callGiftDrawer.classList.toggle('hidden');
        });
    }

    // Dynamic gifts loading from server
    async function fetchGifts() {
        const url = config.dataset.giftsUrl || '/api/gifts/';
        try {
            const data = await api(url);
            if (data.status === 'success' && data.gifts && data.gifts.length > 0) {
                renderGiftsDrawer(data.gifts);
            }
        } catch (err) {
            console.warn('Could not load dynamic gifts', err);
        }
    }

    function renderGiftsDrawer(gifts) {
        const grid = document.getElementById('gifts-grid-container') || callGiftDrawer?.querySelector('.grid');
        if (!grid || !gifts.length) return;
        grid.innerHTML = gifts.map(g => {
            giftIcons[g.slug] = g.icon;
            giftNames[g.slug] = g.name;
            return `
            <button type="button" class="gift-btn flex flex-col items-center gap-1 rounded-2xl border border-white/10 bg-white/10 p-2 transition hover:bg-pink-500/30 hover:scale-105 active:scale-95" data-gift-type="${g.slug}" data-cost="${g.coin_cost}">
                <span class="text-2xl">${g.icon}</span>
                <span class="text-[10px] font-bold text-white truncate max-w-full">${g.name}</span>
                <span class="text-[10px] font-black text-amber-300">${g.coin_cost} 🪙</span>
            </button>`;
        }).join('');

        grid.querySelectorAll('.gift-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const giftType = btn.dataset.giftType;
                const cost = Number(btn.dataset.cost);
                sendGift(giftType, cost);
            });
        });
    }

    // Gift buttons click listeners (initial fallback)
    document.querySelectorAll('.gift-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const giftType = btn.dataset.giftType;
            const cost = Number(btn.dataset.cost);
            sendGift(giftType, cost);
        });
    });

    // Speed Match / Quick Match Function
    async function quickMatch() {
        const quickUrl = config.dataset.quickMatchUrl || '/api/calls/quick-match/';
        if (userCoins < 20 && !isSexCallPremium) {
            toast('At least 20 coins are required for Speed Match. Opening Coin Store...', true);
            openCoinStore();
            return;
        }
        toast('Searching for an online host... 🔍');
        try {
            const res = await api(quickUrl, {method: 'POST', body: '{}'});
            if (res.status === 'success' && res.room_id) {
                activeCall = res.call;
                isCaller = true;
                if (res.call.caller_coins !== undefined) {
                    userCoins = res.call.caller_coins;
                    updateWalletUI();
                }
                showRoom(`Connecting to ${res.host?.username || 'Host'}…`);
                playOutgoingRing();
                await createPeer();
                const offer = await peer.createOffer();
                await peer.setLocalDescription(offer);
                await sendSignal('offer', peer.localDescription.toJSON());
                startPolling();
            }
        } catch (err) {
            toast(err.message || 'No hosts are available right now. Please try again!');
        }
    }

    // Global click delegation for [data-call-target-id] and [data-quick-match-trigger]
    document.addEventListener('click', event => {
        const callBtn = event.target.closest('[data-call-target-id]');
        if (callBtn) {
            event.preventDefault();
            const targetId = callBtn.getAttribute('data-call-target-id');
            if (targetId) initiateCall(targetId);
        }
        const quickBtn = event.target.closest('[data-quick-match-trigger]');
        if (quickBtn) {
            event.preventDefault();
            quickMatch();
        }
    });

    // Coin Store Buttons
    if (openCoinStoreBtn) openCoinStoreBtn.addEventListener('click', openCoinStore);
    if (closeCoinStoreBtn) closeCoinStoreBtn.addEventListener('click', closeCoinStore);
    if (callQuickAddCoins) callQuickAddCoins.addEventListener('click', openCoinStore);
    if (callBannerTopupBtn) callBannerTopupBtn.addEventListener('click', openCoinStore);

    // Incoming Call Checker
    async function checkIncomingCalls() {
        if (activeCall || document.visibilityState !== 'visible') return;
        try {
            const response = await api(config.dataset.incomingUrl);
            if (response.call) showIncoming(response.call);
            else if (incomingRoomId) hideIncoming();
        } catch (error) {
            console.warn('Could not check for incoming calls.', error);
        }
    }
    window.setInterval(checkIncomingCalls, 3500);

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            checkIncomingCalls();
            if (activeCall) {
                pollSignals();
                pollCallStatus();
            }
        }
    });

    // Draggable local video pip
    if (localVideo) {
        localVideo.addEventListener('pointerdown', event => {
            localVideo.setPointerCapture(event.pointerId);
            const startX = event.clientX;
            const startY = event.clientY;
            const rect = localVideo.getBoundingClientRect();
            const move = moveEvent => {
                const left = Math.max(8, Math.min(window.innerWidth - rect.width - 8, rect.left + moveEvent.clientX - startX));
                const top = Math.max(8, Math.min(window.innerHeight - rect.height - 100, rect.top + moveEvent.clientY - startY));
                localVideo.style.left = `${left}px`;
                localVideo.style.top = `${top}px`;
                localVideo.style.right = 'auto';
            };
            const stop = () => {
                localVideo.removeEventListener('pointermove', move);
                localVideo.removeEventListener('pointerup', stop);
                localVideo.removeEventListener('pointercancel', stop);
            };
            localVideo.addEventListener('pointermove', move);
            localVideo.addEventListener('pointerup', stop);
            localVideo.addEventListener('pointercancel', stop);
        });
    }

    // Initialize on load
    fetchWallet();
    fetchCoinPackages();
    fetchGifts();

    // Export global helpers
    window.lovenyStartVideoCall = initiateCall;
    window.lovenyOpenCoinStore = openCoinStore;
    window.lovenyQuickMatch = quickMatch;
})();

