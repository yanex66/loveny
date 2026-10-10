(() => {
    'use strict';

    const visionVersion = '0.10.21';
    const visionBase = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${visionVersion}`;
    const faceModelUrl = 'https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite';

    window.CallPrivacy = {
        active: false,
        callId: null,
        viewerId: null,
        localVerified: false,
        peerVerified: false,
        handshakeComplete: false,
        focusLocked: false,
        captureLocked: false,
        peerPaused: false,
        _unblurTimeout: null,
        _driftAnimationId: null,
        _faceAnimationId: null,
        _socket: null,
        _faceDetector: null,
        _pendingRemoteStream: null,
        onFeedsUnlocked: null,
        _listeners: [],

        init(callId, viewerId) {
            if (this.active) this.cleanup();
            this.callId = callId;
            this.viewerId = viewerId;
            this.active = true;
            this.localVerified = false;
            this.peerVerified = false;
            this.handshakeComplete = false;
            this.focusLocked = false;
            this.captureLocked = false;
            this.peerPaused = false;
            this._pendingRemoteStream = null;

            this.injectStyles();
            this.setupInteractionGuards();
            this.setupTabFocusGuard();
            this.setupNativeCaptureObserver();
            this.setupWatermarkCanvas();
            this.setupHandshakeOverlay();
            this.connectSignalingSocket();
        },

        listen(target, eventName, handler, options) {
            target.addEventListener(eventName, handler, options);
            this._listeners.push({target, eventName, handler, options});
        },

        injectStyles() {
            if (document.getElementById('call-privacy-styles')) return;
            const style = document.createElement('style');
            style.id = 'call-privacy-styles';
            style.textContent = `
                #video-call-room,
                main[aria-label="1-on-1 Video Call"],
                .call-display,
                #video-call-room video,
                main[aria-label="1-on-1 Video Call"] video,
                .call-display video,
                img.call-avatar,
                img.user-avatar {
                    -webkit-touch-callout: none !important;
                    -webkit-user-select: none !important;
                    user-select: none !important;
                }
                .privacy-obscured {
                    filter: blur(45px) brightness(0) !important;
                    transition: filter 0.15s ease-out !important;
                }
                .privacy-clear {
                    filter: none !important;
                    transition: filter 0.5s ease-in !important;
                }
                #watermark-canvas {
                    position: absolute;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    pointer-events: none;
                    z-index: 15;
                }
                #remote-watermark-layer {
                    position: absolute;
                    inset: 0;
                    z-index: 10;
                    pointer-events: none;
                }
                #call-privacy-blur-card,
                #face-handshake-overlay {
                    position: absolute;
                    inset: 0;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    color: #fff;
                    text-align: center;
                    padding: 1.5rem;
                }
                #call-privacy-blur-card {
                    z-index: 30;
                    background: rgba(10, 5, 20, 0.98);
                    backdrop-filter: blur(20px);
                }
                #face-handshake-overlay {
                    z-index: 35;
                    background: rgba(15, 7, 28, 0.92);
                    backdrop-filter: blur(28px);
                    transition: opacity 0.6s ease, transform 0.6s ease;
                }
            `;
            document.head.appendChild(style);
        },

        getRemoteVideo() {
            return document.getElementById('remote-call-video') ||
                document.getElementById('remote-video');
        },

        getLocalVideo() {
            return document.getElementById('local-call-video') ||
                document.getElementById('local-video');
        },

        getContainer() {
            return document.getElementById('video-call-room') ||
                document.querySelector('main[aria-label="1-on-1 Video Call"]') ||
                document.body;
        },

        csrfToken() {
            const token = document.cookie.split('; ').find(row => row.startsWith('csrftoken='));
            return token ? decodeURIComponent(token.split('=').slice(1).join('=')) : '';
        },

        connectSignalingSocket() {
            if (!this.callId || !window.WebSocket) return;
            const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            this._socket = new WebSocket(
                `${scheme}//${window.location.host}/ws/calls/${this.callId}/`
            );
            this._socket.addEventListener('message', event => {
                try {
                    const message = JSON.parse(event.data);
                    if (message.type !== 'error' && message.type) {
                        this.onPeerSignal(message.type, message.payload || {});
                    }
                } catch (error) {
                    console.error('[CallPrivacy] Invalid signaling event.', error);
                }
            });
            this._socket.addEventListener('error', () => {
                console.warn('[CallPrivacy] Signaling WebSocket is unavailable.');
            });
        },

        async sendSignal(signalType, payload = {}) {
            if (!this.callId) return false;
            const message = {
                type: signalType,
                payload: {
                    call_id: this.callId,
                    user_id: this.viewerId,
                    timestamp: new Date().toISOString(),
                    ...payload,
                },
            };
            if (this._socket && this._socket.readyState === WebSocket.OPEN) {
                this._socket.send(JSON.stringify(message));
                return true;
            }

            try {
                const response = await fetch(`/api/calls/${this.callId}/signals/send/`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': this.csrfToken(),
                    },
                    body: JSON.stringify(message),
                });
                if (!response.ok) {
                    console.error(`[CallPrivacy] Signaling failed (${response.status}).`);
                    return false;
                }
                return true;
            } catch (error) {
                console.error(`[CallPrivacy] Failed to emit ${signalType}.`, error);
                return false;
            }
        },

        setupInteractionGuards() {
            const isMediaTarget = target => target instanceof Element && Boolean(
                target.closest('video, img, #video-call-room, main[aria-label="1-on-1 Video Call"], .call-display, [data-call-display]')
            );
            this.listen(window, 'contextmenu', event => {
                if (this.active && isMediaTarget(event.target)) event.preventDefault();
            }, {passive: false});
            this.listen(window, 'dragstart', event => {
                if (this.active && isMediaTarget(event.target)) event.preventDefault();
            }, {passive: false});
            this.listen(window, 'selectstart', event => {
                if (this.active && isMediaTarget(event.target)) event.preventDefault();
            }, {passive: false});
        },

        setupTabFocusGuard() {
            const handleBlur = () => {
                if (!this.active) return;
                clearTimeout(this._unblurTimeout);
                this.focusLocked = true;
                this.refreshPrivacyState();
                this.sendSignal('privacy_blur_engaged');
            };
            const handleFocus = () => {
                if (!this.active) return;
                clearTimeout(this._unblurTimeout);
                this._unblurTimeout = setTimeout(() => {
                    this.focusLocked = false;
                    this.refreshPrivacyState();
                    this.sendSignal('privacy_blur_lifted');
                }, 500);
            };
            this.listen(window, 'blur', handleBlur);
            this.listen(window, 'focus', handleFocus);
            this.listen(document, 'visibilitychange', () => {
                if (document.hidden) handleBlur();
                else handleFocus();
            });
        },

        setupNativeCaptureObserver() {
            this.listen(window, 'captureStatusChanged', event => {
                const detail = event.detail || {};
                this.captureLocked = Boolean(detail.isRecording || detail.isCaptured);
                this.refreshPrivacyState();
                if (this.captureLocked) {
                    this.sendSignal('privacy_blur_engaged', {reason: 'native_capture'});
                } else {
                    this.sendSignal('privacy_blur_lifted', {reason: 'native_capture'});
                }
            });
        },

        refreshPrivacyState() {
            const isPrivacyLocked = this.focusLocked || this.captureLocked || this.peerPaused;
            [this.getRemoteVideo(), this.getLocalVideo()].forEach(video => {
                if (!video) return;
                if (isPrivacyLocked || !this.handshakeComplete) {
                    video.classList.remove('privacy-clear');
                    video.classList.add('privacy-obscured');
                } else {
                    video.classList.remove('privacy-obscured');
                    video.classList.add('privacy-clear');
                }
            });
            if (
                this.handshakeComplete &&
                !isPrivacyLocked &&
                this._pendingRemoteStream
            ) {
                const remote = this.getRemoteVideo();
                if (remote) {
                    remote.srcObject = this._pendingRemoteStream;
                    remote.play().catch(error => {
                        console.warn('Remote video playback requires interaction.', error);
                    });
                    this._pendingRemoteStream = null;
                }
            }

            if (this.focusLocked || this.captureLocked) {
                this.showBlurCard(
                    this.captureLocked ? 'Recording Detected' : 'Video Feed Locked',
                    this.captureLocked
                        ? 'Screen recording or mirroring is active. The video feed is hidden.'
                        : 'Video feed locked — return to active call window'
                );
            } else if (this.peerPaused) {
                this.showBlurCard(
                    'Partner Paused',
                    'Your partner switched tabs or minimized the app. The stream is temporarily paused.'
                );
            } else {
                this.hideBlurCard();
            }
        },

        setupWatermarkCanvas() {
            const remote = this.getRemoteVideo();
            const container = remote?.parentElement || this.getContainer();
            let layer = document.getElementById('remote-watermark-layer');
            if (!layer && remote?.parentNode) {
                layer = document.createElement('div');
                layer.id = 'remote-watermark-layer';
                remote.parentNode.insertBefore(layer, remote);
                layer.appendChild(remote);
                this._watermarkLayer = layer;
                this._remoteParent = layer.parentElement;
            }
            const drawingArea = layer || container;
            let canvas = document.getElementById('watermark-canvas');
            if (!canvas) {
                canvas = document.createElement('canvas');
                canvas.id = 'watermark-canvas';
                drawingArea.appendChild(canvas);
            }
            const context = canvas.getContext('2d');
            if (!context) {
                console.error('[CallPrivacy] Could not create the forensic watermark canvas.');
                return;
            }

            const resize = () => {
                const bounds = drawingArea.getBoundingClientRect();
                const scale = window.devicePixelRatio || 1;
                canvas.width = Math.max(1, Math.round(bounds.width * scale));
                canvas.height = Math.max(1, Math.round(bounds.height * scale));
                context.setTransform(scale, 0, 0, scale, 0, 0);
            };
            this.listen(window, 'resize', resize);
            resize();

            let startTime = performance.now();
            const render = now => {
                if (!this.active) return;
                const bounds = drawingArea.getBoundingClientRect();
                const scale = window.devicePixelRatio || 1;
                const width = bounds.width;
                const height = bounds.height;
                if (canvas.width !== Math.round(width * scale) ||
                    canvas.height !== Math.round(height * scale)) resize();
                context.clearRect(0, 0, width, height);
                const cycle = ((now - startTime) % 9000) / 9000;
                const driftX = Math.sin(cycle * Math.PI * 2) * 24;
                const driftY = Math.cos(cycle * Math.PI * 2) * 16;
                const timestamp = `${new Date().toISOString().replace('T', ' ').slice(0, 19)} UTC`;
                const label = `VIEWER: ${this.viewerId} | CALL: ${this.callId} | ${timestamp}`;

                context.save();
                context.font = 'bold 11px monospace';
                context.fillStyle = 'rgba(255, 255, 255, 0.22)';
                context.shadowColor = 'rgba(0, 0, 0, 0.35)';
                context.shadowBlur = 3;
                for (let x = -260 + driftX; x < width + 260; x += 260) {
                    for (let y = -140 + driftY; y < height + 140; y += 140) {
                        context.save();
                        context.translate(x, y);
                        context.rotate(-18 * Math.PI / 180);
                        context.fillText(label, 0, 0);
                        context.restore();
                    }
                }
                context.restore();
                this._driftAnimationId = requestAnimationFrame(render);
            };
            this._driftAnimationId = requestAnimationFrame(render);
        },

        setupHandshakeOverlay() {
            const container = this.getContainer();
            let overlay = document.getElementById('face-handshake-overlay');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.id = 'face-handshake-overlay';
                container.appendChild(overlay);
            }
            overlay.innerHTML = `
                <div style="max-width: 320px; width: 100%;">
                    <div id="handshake-icon" style="font-size: 3rem; margin-bottom: 0.75rem;">👤</div>
                    <h2 id="handshake-title" style="font-size: 1.15rem; font-weight: 900; margin-bottom: 0.25rem;">Verifying mutual presence &amp; security...</h2>
                    <p id="handshake-desc" style="font-size: 0.8125rem; opacity: 0.8; margin-bottom: 1.25rem;">Keep exactly one face in front of the camera for 3 seconds.</p>
                    <div style="background: rgba(255,255,255,0.15); border-radius: 9999px; height: 8px; overflow: hidden; margin-bottom: 0.75rem;">
                        <div id="handshake-progress" style="width: 0%; height: 100%; background: linear-gradient(90deg, #ec4899, #f59e0b); transition: width 0.2s ease;"></div>
                    </div>
                    <div id="handshake-status" style="font-size: 0.75rem; font-weight: 700; color: #f472b6;">Starting on-device face detection…</div>
                </div>
            `;
            this.refreshPrivacyState();
        },

        async startFaceVerification(localStream) {
            if (this.localVerified) return;
            const video = this.getLocalVideo();
            if (!video) {
                console.error('[CallPrivacy] Local video element is missing.');
                return;
            }
            if (!video.srcObject) video.srcObject = localStream;
            video.muted = true;
            try {
                await video.play();
                const vision = await import(`${visionBase}/vision_bundle.mjs`);
                const fileset = await vision.FilesetResolver.forVisionTasks(`${visionBase}/wasm`);
                this._faceDetector = await vision.FaceDetector.createFromOptions(fileset, {
                    baseOptions: {modelAssetPath: faceModelUrl, delegate: 'CPU'},
                    runningMode: 'VIDEO',
                    minDetectionConfidence: 0.65,
                });
            } catch (error) {
                console.error('[CallPrivacy] On-device face detection could not start.', error);
                const status = document.getElementById('handshake-status');
                if (status) status.textContent = 'Face detection is unavailable. Please reload and try again.';
                return;
            }

            let facePresentSince = null;
            let lastDetectionAt = 0;
            const update = now => {
                if (!this.active || this.localVerified) return;
                if (now - lastDetectionAt >= 200) {
                    lastDetectionAt = now;
                    try {
                        const result = this._faceDetector.detectForVideo(video, now);
                        const singleFace = result.detections.length === 1;
                        if (singleFace) {
                            if (facePresentSince === null) facePresentSince = now;
                        } else {
                            facePresentSince = null;
                        }
                    } catch (error) {
                        facePresentSince = null;
                        console.error('[CallPrivacy] Face detection frame failed.', error);
                    }
                }

                const elapsed = facePresentSince === null ? 0 : now - facePresentSince;
                const progress = Math.min(100, Math.round((elapsed / 3000) * 100));
                const progressEl = document.getElementById('handshake-progress');
                const statusEl = document.getElementById('handshake-status');
                if (progressEl) progressEl.style.width = `${progress}%`;
                if (statusEl) {
                    statusEl.textContent = facePresentSince === null
                        ? 'Keep exactly one face clearly in frame.'
                        : `Hold still for ${(Math.max(0, 3 - elapsed / 1000)).toFixed(1)}s`;
                }
                if (elapsed >= 3000) {
                    this.onLocalFaceVerified();
                    return;
                }
                this._faceAnimationId = requestAnimationFrame(update);
            };
            this._faceAnimationId = requestAnimationFrame(update);
        },

        onLocalFaceVerified() {
            if (this.localVerified) return;
            this.localVerified = true;
            cancelAnimationFrame(this._faceAnimationId);
            const icon = document.getElementById('handshake-icon');
            const status = document.getElementById('handshake-status');
            const title = document.getElementById('handshake-title');
            const description = document.getElementById('handshake-desc');
            if (icon) icon.textContent = '✅';
            if (title) title.textContent = 'You are verified';
            if (description) description.textContent = 'Waiting for your partner and the server to confirm both checks.';
            if (status) status.textContent = 'Waiting for mutual verification…';
            this.sendSignal('face_verified', {verified: true});
        },

        onPeerSignal(signalType, payload) {
            if (signalType === 'face_verified') {
                this.peerVerified = true;
                return;
            }
            if (signalType === 'feeds_unlocked' && payload.unlocked === true) {
                this.peerVerified = true;
                this.unlockFeeds();
            } else if (signalType === 'privacy_blur_engaged') {
                this.peerPaused = true;
                this.refreshPrivacyState();
            } else if (signalType === 'privacy_blur_lifted') {
                this.peerPaused = false;
                this.refreshPrivacyState();
            }
        },

        unlockFeeds() {
            if (this.handshakeComplete) return;
            this.handshakeComplete = true;
            const overlay = document.getElementById('face-handshake-overlay');
            if (overlay) {
                overlay.style.opacity = '0';
                overlay.style.transform = 'scale(1.05)';
                setTimeout(() => overlay.remove(), 600);
            }
            this.refreshPrivacyState();
            if (this.onFeedsUnlocked) {
                Promise.resolve(this.onFeedsUnlocked()).catch(error => {
                    console.error('[CallPrivacy] Could not enable verified media tracks.', error);
                });
            }
        },

        setRemoteStream(stream) {
            this._pendingRemoteStream = stream;
            this.refreshPrivacyState();
        },

        showBlurCard(title, message) {
            const container = this.getContainer();
            let card = document.getElementById('call-privacy-blur-card');
            if (!card) {
                card = document.createElement('div');
                card.id = 'call-privacy-blur-card';
                container.appendChild(card);
            }
            card.textContent = '';
            const heading = document.createElement('h3');
            heading.textContent = title;
            const content = document.createElement('p');
            content.textContent = message;
            card.append(heading, content);
        },

        hideBlurCard() {
            const card = document.getElementById('call-privacy-blur-card');
            if (card) card.remove();
        },

        cleanup() {
            this.active = false;
            cancelAnimationFrame(this._driftAnimationId);
            cancelAnimationFrame(this._faceAnimationId);
            clearTimeout(this._unblurTimeout);
            this._listeners.forEach(({target, eventName, handler, options}) => {
                target.removeEventListener(eventName, handler, options);
            });
            this._listeners = [];
            if (this._socket) {
                this._socket.close();
                this._socket = null;
            }
            if (this._faceDetector) {
                this._faceDetector.close();
                this._faceDetector = null;
            }
            this._pendingRemoteStream = null;
            this.onFeedsUnlocked = null;
            const remote = this.getRemoteVideo();
            if (this._watermarkLayer && remote && this._remoteParent) {
                this._remoteParent.insertBefore(remote, this._watermarkLayer);
                this._watermarkLayer.remove();
                this._watermarkLayer = null;
                this._remoteParent = null;
            }
            ['watermark-canvas', 'remote-watermark-layer', 'call-privacy-blur-card', 'face-handshake-overlay'].forEach(id => {
                document.getElementById(id)?.remove();
            });
        },
    };
})();
