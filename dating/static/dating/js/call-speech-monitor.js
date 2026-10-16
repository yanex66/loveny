(() => {
    'use strict';
    if (window.CallSpeechMonitor) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recognition = null;
    let callId = null;
    let csrfToken = '';
    let onFlagged = () => {};
    let onUnavailable = () => {};
    let active = false;
    let paused = false;
    let detectionEnabled = true;
    let submitting = false;
    let startupTimer = null;
    let restartTimer = null;
    let consecutiveErrors = 0;
    let rejectStartup = null;
    const seenTranscripts = new Set();
    const maxRestartAttempts = 4;

    function failMonitoring(message) {
        if (startupTimer !== null) {
            window.clearTimeout(startupTimer);
            startupTimer = null;
            const reject = rejectStartup;
            rejectStartup = null;
            if (reject) reject(new Error(message));
            return;
        }
        stop();
        onUnavailable(message);
    }

    function scheduleRestart(message) {
        if (!active || paused || submitting || restartTimer !== null) return;
        consecutiveErrors += 1;
        if (consecutiveErrors > maxRestartAttempts) {
            failMonitoring(message);
            return;
        }
        const delay = Math.min(1000 * (2 ** (consecutiveErrors - 1)), 8000);
        restartTimer = window.setTimeout(() => {
            restartTimer = null;
            startRecognition();
        }, delay);
    }

    function hasContactExchangeIndicators(text) {
        const normalized = text.toLowerCase();
        const digits = normalized.replace(/\D/g, '');
        if (digits.length >= 7) return true;
        if (/\bmy\s+(?:phone\s+)?number\s+is\b/i.test(text)) return true;
        const numberWords = new Set([
            'zero', 'oh', 'o', 'one', 'two', 'three', 'four', 'five',
            'six', 'seven', 'eight', 'nine',
        ]);
        let consecutiveNumberWords = 0;
        for (const word of normalized.match(/\b[a-z]+\b/g) || []) {
            consecutiveNumberWords = numberWords.has(word)
                ? consecutiveNumberWords + 1
                : 0;
            if (consecutiveNumberWords >= 7) return true;
        }
        if (/\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|\b(?:https?:\/\/|www\.)\S+/i.test(text)) {
            return true;
        }

        const contactChannel = /\b(?:whats?\s*app|telegram|instagram|insta|snapchat|snap|tiktok|facebook|phone|number|contact|handle|username|email|e-mail|dm)\b/i;
        const exchangeIntent = /\b(?:send|share|give|drop|text|call|message|reach|contact|add|follow|dm|what(?:'s| is)|whats|can i get|do you have)\b/i;
        return contactChannel.test(text) && exchangeIntent.test(text);
    }

    function startRecognition() {
        if (!active || paused || submitting || !recognition) return;
        try {
            recognition.start();
        } catch (error) {
            if (error.name !== 'InvalidStateError') {
                scheduleRestart(
                    'Speech monitoring could not restart after several attempts. The call was ended to keep monitoring active.',
                );
            }
        }
    }

    function stop() {
        active = false;
        paused = false;
        window.clearTimeout(startupTimer);
        window.clearTimeout(restartTimer);
        startupTimer = null;
        restartTimer = null;
        consecutiveErrors = 0;
        if (rejectStartup) {
            rejectStartup(new Error('Live-call speech monitoring stopped before it became ready.'));
            rejectStartup = null;
        }
        if (recognition) {
            recognition.onend = null;
            recognition.onerror = null;
            recognition.onresult = null;
            try {
                recognition.abort();
            } catch (error) {
                console.warn('Could not stop live-call speech monitoring.', error);
            }
            recognition = null;
        }
        seenTranscripts.clear();
    }

    async function submitCandidate(transcript) {
        if (submitting || !active) return;
        submitting = true;
        try {
            const response = await fetch(`/api/calls/${encodeURIComponent(callId)}/moderation-flag/`, {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: JSON.stringify({transcript: transcript.slice(0, 500)}),
            });
            let result;
            try {
                if (!(response.headers.get('content-type') || '').includes('application/json')) {
                    throw new TypeError('Response was not JSON.');
                }
                result = await response.json();
            } catch (parseError) {
                console.error('Call moderation returned an invalid response.', {
                    status: response.status,
                    url: response.url,
                    redirected: response.redirected,
                    parseError,
                });
                throw new Error(
                    response.redirected
                        ? 'Your session expired, so live-call safety monitoring stopped. The call was ended.'
                        : 'The live-call safety service returned an unexpected response. The call was ended.',
                );
            }
            if (response.status === 201 && result.status === 'suspended') {
                stop();
                onFlagged(result);
            } else if (response.status === 409 && result.message === 'call_not_active') {
                stop();
            } else if (!response.ok) {
                throw new Error(result.message || 'The call monitoring service is unavailable.');
            }
        } catch (error) {
            stop();
            onUnavailable(
                error.message || 'Live-call speech monitoring stopped. The call was ended.',
            );
        } finally {
            submitting = false;
            if (active && !paused) startRecognition();
        }
    }

    window.CallSpeechMonitor = {
        supported: Boolean(SpeechRecognition),

        requestConsent() {
            return new Promise(resolve => {
                const overlay = document.createElement('div');
                overlay.className = 'fixed inset-0 z-[200] flex items-center justify-center bg-slate-950/70 p-5 backdrop-blur-sm';
                overlay.setAttribute('role', 'presentation');
                const supported = Boolean(SpeechRecognition);
                overlay.innerHTML = `
                    <section role="dialog" aria-modal="true" aria-labelledby="call-monitor-title"
                        class="w-full max-w-sm rounded-3xl border border-pink-200/20 bg-slate-900 p-5 text-white shadow-2xl">
                        <div class="mb-3 flex h-10 w-10 items-center justify-center rounded-2xl bg-pink-500/15 text-pink-300" aria-hidden="true">
                            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                                <path d="M19 10v1a7 7 0 0 1-14 0v-1m7 8v4m-4 0h8"/>
                            </svg>
                        </div>
                        <h2 id="call-monitor-title" class="text-lg font-black">${supported ? 'A quick note before your call' : 'Call monitoring unavailable'}</h2>
                        <p class="mt-2 text-sm leading-relaxed text-slate-300">${
                            supported
                                ? 'Live-call monitoring checks for attempts to share contact details. Your browser’s speech recognition provider may process audio; LOVENY does not record calls or keep full transcripts. Detected contact sharing can end the call and suspend the account for review.'
                                : 'This browser does not support the speech monitoring required for live calls. Try a supported browser to continue.'
                        }</p>
                        <div class="mt-5 flex gap-3">
                            <button type="button" data-consent="no" class="flex-1 rounded-xl border border-white/15 px-3 py-3 text-sm font-bold text-slate-200 hover:bg-white/10">${supported ? 'Not now' : 'Close'}</button>
                            ${supported ? '<button type="button" data-consent="yes" class="flex-1 rounded-xl bg-gradient-to-r from-pink-600 to-violet-600 px-3 py-3 text-sm font-black text-white hover:brightness-110">I agree &amp; continue</button>' : ''}
                        </div>
                    </section>`;
                const onKeydown = event => {
                    if (event.key === 'Escape') finish(false);
                };
                const finish = accepted => {
                    overlay.remove();
                    document.removeEventListener('keydown', onKeydown);
                    resolve(accepted);
                };
                overlay.querySelector('[data-consent="no"]').addEventListener('click', () => finish(false));
                overlay.querySelector('[data-consent="yes"]')?.addEventListener('click', () => finish(true));
                overlay.addEventListener('click', event => {
                    if (event.target === overlay) finish(false);
                });
                document.addEventListener('keydown', onKeydown);
                document.body.appendChild(overlay);
                overlay.querySelector('[data-consent="yes"], [data-consent="no"]').focus();
            });
        },

        async start(activeCallId, token, callbacks = {}) {
            if (!SpeechRecognition) {
                throw new Error('Live-call speech monitoring is not supported in this browser.');
            }
            stop();
            callId = activeCallId;
            csrfToken = token;
            onFlagged = callbacks.onFlagged || (() => {});
            onUnavailable = callbacks.onUnavailable || (() => {});
            detectionEnabled = callbacks.waitForConnection !== true;
            active = true;
            recognition = new SpeechRecognition();
            recognition.continuous = true;
            recognition.interimResults = false;
            recognition.lang = 'en-US';
            const recognitionStarted = new Promise((resolve, reject) => {
                rejectStartup = reject;
                startupTimer = window.setTimeout(() => {
                    rejectStartup = null;
                    reject(new Error('Speech monitoring did not start after several attempts. The call was ended.'));
                }, 15000);
                recognition.onstart = () => {
                    window.clearTimeout(startupTimer);
                    startupTimer = null;
                    rejectStartup = null;
                    resolve();
                };
            });

            recognition.onresult = event => {
                for (let index = event.resultIndex; index < event.results.length; index += 1) {
                    const result = event.results[index];
                    if (!result.isFinal) continue;
                    consecutiveErrors = 0;
                    const transcript = result[0]?.transcript?.trim();
                    if (
                        detectionEnabled
                        && transcript
                        && !seenTranscripts.has(transcript)
                        && hasContactExchangeIndicators(transcript)
                    ) {
                        seenTranscripts.add(transcript);
                        submitCandidate(transcript);
                        return;
                    }
                }
            };
            recognition.onerror = event => {
                if (!active || paused || event.error === 'no-speech' || event.error === 'aborted') {
                    return;
                }
                if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                    failMonitoring(
                        'Live-call speech monitoring permission was denied. The call was ended.',
                    );
                    return;
                }
                scheduleRestart(
                    'Live-call speech monitoring stopped unexpectedly after several restart attempts. The call was ended.',
                );
            };
            recognition.onend = () => {
                if (active && !paused && !submitting && restartTimer === null) {
                    restartTimer = window.setTimeout(() => {
                        restartTimer = null;
                        startRecognition();
                    }, 350);
                }
            };
            startRecognition();
            try {
                await recognitionStarted;
                const response = await fetch(`/api/calls/${encodeURIComponent(callId)}/signals/send/`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    cache: 'no-store',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken,
                    },
                    body: JSON.stringify({
                        type: 'speech_monitor_ready',
                        payload: {ready: true},
                    }),
                });
                if (!response.ok) {
                    throw new Error('The server could not confirm call speech monitoring.');
                }
            } catch (error) {
                stop();
                throw error;
            }
            return true;
        },

        enableDetection() {
            detectionEnabled = true;
        },

        pause() {
            if (!active || paused || !recognition) return;
            paused = true;
            try {
                recognition.stop();
            } catch (error) {
                console.warn('Could not pause live-call speech monitoring.', error);
            }
        },

        resume() {
            if (!active || !paused) return;
            paused = false;
            startRecognition();
        },

        stop,
    };
})();
