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
            const result = await response.json();
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
            if (!SpeechRecognition) {
                window.alert(
                    'Live-call speech monitoring is not supported by this browser. ' +
                    'Use a supported browser to start a monitored video call.',
                );
                return false;
            }
            return window.confirm(
                'Live-call speech monitoring is active. Your browser speech-recognition ' +
                'provider may process microphone audio outside LOVENY. LOVENY does not ' +
                'record call audio or retain full transcripts. A short detected excerpt ' +
                'is checked for contact-sharing and is not kept in the moderation report. ' +
                'A likely attempt ends the call and immediately suspends the account ' +
                'pending admin review. Continue?',
            );
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
