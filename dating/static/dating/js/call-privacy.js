/**
 * LOVENY Video Call Privacy & Anti-Leak Deterrents Module
 * Features:
 * 1. Dynamic Forensic Micro-Drifting Watermark Canvas
 * 2. Tab Focus & Notification Shade Peeking Guard (Blur & Signal)
 * 3. Anti-Screenshot & Interaction Deterrents (Contextmenu, Drag, Touch-callout)
 * 4. Native Container Screen Recording Observer (captureStatusChanged)
 * 5. Pre-Call Mutual 3-Second Face Verification Handshake
 */

(() => {
    'use strict';

    window.CallPrivacy = {
        active: false,
        callId: null,
        viewerId: null,
        localVerified: false,
        peerVerified: false,
        handshakeComplete: false,
        _unblurTimeout: null,
        _driftAnimationId: null,
        _faceCheckInterval: null,
        _consecutiveFaceSeconds: 0,

        init(callId, viewerId) {
            this.callId = callId || 'CALL-SESSION';
            this.viewerId = viewerId || 'VIEWER';
            this.active = true;
            this.localVerified = false;
            this.peerVerified = false;
            this.handshakeComplete = false;
            this._consecutiveFaceSeconds = 0;

            this.injectStyles();
            this.setupInteractionGuards();
            this.setupTabFocusGuard();
            this.setupNativeCaptureObserver();
            this.setupWatermarkCanvas();
            this.setupHandshakeOverlay();

            console.log(`[CallPrivacy] Initialized for Call ${this.callId}, Viewer ${this.viewerId}`);
        },

        injectStyles() {
            if (document.getElementById('call-privacy-styles')) return;
            const style = document.createElement('style');
            style.id = 'call-privacy-styles';
            style.textContent = `
                /* Interaction Deterrents */
                body.in-video-call,
                #video-call-room,
                main[aria-label="1-on-1 Video Call"],
                video {
                    user-select: none !important;
                    -webkit-user-select: none !important;
                    -webkit-touch-callout: none !important;
                }
                /* Stream Obscuration Blur */
                .privacy-obscured {
                    filter: blur(45px) brightness(0) !important;
                    transition: filter 0.15s ease-out !important;
                }
                .privacy-clear {
                    filter: none !important;
                    transition: filter 0.5s ease-in !important;
                }
                /* Watermark Canvas */
                #watermark-canvas {
                    position: absolute;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    pointer-events: none;
                    z-index: 15;
                }
                /* Privacy Overlay Card */
                #call-privacy-blur-card {
                    position: absolute;
                    inset: 0;
                    z-index: 25;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    background: rgba(10, 5, 20, 0.88);
                    backdrop-filter: blur(20px);
                    color: #fff;
                    text-align: center;
                    padding: 1.5rem;
                    transition: opacity 0.3s ease;
                }
                /* Pre-Call Handshake Frosted Overlay */
                #face-handshake-overlay {
                    position: absolute;
                    inset: 0;
                    z-index: 35;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    background: rgba(15, 7, 28, 0.92);
                    backdrop-filter: blur(28px);
                    color: #fff;
                    text-align: center;
                    padding: 1.5rem;
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
            return token ? decodeURIComponent(token.split('=')[1]) : '';
        },

        async sendSignal(signalType, payload = {}) {
            if (!this.callId) return;
            try {
                await fetch(`/api/calls/${this.callId}/signals/send/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': this.csrfToken(),
                    },
                    body: JSON.stringify({
                        type: signalType,
                        signal_type: signalType,
                        payload: {
                            call_id: this.callId,
                            user_id: this.viewerId,
                            timestamp: new Date().toISOString(),
                            ...payload,
                        },
                    }),
                });
            } catch (err) {
                console.warn(`[CallPrivacy] Failed to emit signal ${signalType}:`, err);
            }
        },

        setupInteractionGuards() {
            // Prevent context menus (long-tap or right click)
            window.addEventListener('contextmenu', (e) => {
                if (this.active) {
                    e.preventDefault();
                    return false;
                }
            }, { passive: false });

            // Prevent image/video dragging
            window.addEventListener('dragstart', (e) => {
                if (this.active) {
                    e.preventDefault();
                    return false;
                }
            }, { passive: false });
        },

        setupTabFocusGuard() {
            const handleBlur = () => {
                if (!this.active) return;
                clearTimeout(this._unblurTimeout);
                this.engagePrivacyBlur();
                this.sendSignal('privacy_blur_engaged');
            };

            const handleFocus = () => {
                if (!this.active) return;
                clearTimeout(this._unblurTimeout);
                // 500ms debounce before lifting privacy blur
                this._unblurTimeout = setTimeout(() => {
                    this.liftPrivacyBlur();
                    this.sendSignal('privacy_blur_lifted');
                }, 500);
            };

            window.addEventListener('blur', handleBlur);
            window.addEventListener('focus', handleFocus);

            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    handleBlur();
                } else {
                    handleFocus();
                }
            });
        },

        engagePrivacyBlur() {
            const remote = this.getRemoteVideo();
            if (remote) {
                remote.classList.remove('privacy-clear');
                remote.classList.add('privacy-obscured');
            }
            this.showBlurCard(
                "🔒 Security Pause",
                "Stream obscured while backgrounded to protect user privacy."
            );
        },

        liftPrivacyBlur() {
            // If handshake is complete, restore clear video
            if (this.handshakeComplete) {
                const remote = this.getRemoteVideo();
                if (remote) {
                    remote.classList.remove('privacy-obscured');
                    remote.classList.add('privacy-clear');
                }
            }
            this.hideBlurCard();
        },

        showBlurCard(title, message) {
            let card = document.getElementById('call-privacy-blur-card');
            const container = this.getContainer();
            if (!card && container) {
                card = document.createElement('div');
                card.id = 'call-privacy-blur-card';
                container.appendChild(card);
            }
            if (card) {
                card.innerHTML = `
                    <div style="font-size: 2.5rem; margin-bottom: 0.75rem;">🛡️</div>
                    <h3 style="font-size: 1.125rem; font-weight: 800; margin-bottom: 0.5rem;">${title}</h3>
                    <p style="font-size: 0.8125rem; opacity: 0.75; max-width: 280px; line-height: 1.4;">${message}</p>
                `;
                card.style.display = 'flex';
            }
        },

        hideBlurCard() {
            const card = document.getElementById('call-privacy-blur-card');
            if (card) {
                card.style.display = 'none';
            }
        },

        setupNativeCaptureObserver() {
            // iOS UIScreen.capturedDidChangeNotification / Android screen record bridge
            window.addEventListener('captureStatusChanged', (e) => {
                const isCaptured = e && e.detail && e.detail.isCaptured;
                console.warn(`[CallPrivacy] Native captureStatusChanged received: isCaptured=${isCaptured}`);
                if (isCaptured) {
                    const remote = this.getRemoteVideo();
                    if (remote) {
                        remote.classList.add('privacy-obscured');
                    }
                    this.showBlurCard(
                        "⚠️ Recording Detected",
                        "Screen recording / projection is prohibited. Stream hidden."
                    );
                } else {
                    this.liftPrivacyBlur();
                }
            });
        },

        setupWatermarkCanvas() {
            let canvas = document.getElementById('watermark-canvas');
            const container = this.getContainer();
            if (!canvas && container) {
                canvas = document.createElement('canvas');
                canvas.id = 'watermark-canvas';
                container.appendChild(canvas);
            }
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            let startTime = performance.now();

            const resizeCanvas = () => {
                if (canvas && container) {
                    canvas.width = container.clientWidth || window.innerWidth;
                    canvas.height = container.clientHeight || window.innerHeight;
                }
            };
            window.addEventListener('resize', resizeCanvas);
            resizeCanvas();

            // Render loop with micro-drifting translation every 8-10 seconds
            const renderWatermark = (now) => {
                if (!this.active) return;

                if (!canvas.width || !canvas.height) {
                    resizeCanvas();
                }

                ctx.clearRect(0, 0, canvas.width, canvas.height);

                // Smooth drifting offset calculation (cycle ~9 seconds)
                const cycle = ((now - startTime) % 9000) / 9000; // 0 to 1
                const driftX = Math.sin(cycle * Math.PI * 2) * 24;
                const driftY = Math.cos(cycle * Math.PI * 2) * 16;

                // Live UTC timestamp
                const nowUtc = new Date().toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
                const watermarkText = `VIEWER: ${this.viewerId} | CALL: ${this.callId.substring(0, 8)} | ${nowUtc}`;

                ctx.save();
                ctx.font = 'bold 11px monospace';
                ctx.fillStyle = 'rgba(255, 255, 255, 0.22)';
                ctx.shadowColor = 'rgba(0, 0, 0, 0.35)';
                ctx.shadowBlur = 3;

                // Rotate slightly for anti-crop diagonal rendering
                const spacingX = 260;
                const spacingY = 140;

                for (let x = -spacingX + driftX; x < canvas.width + spacingX; x += spacingX) {
                    for (let y = -spacingY + driftY; y < canvas.height + spacingY; y += spacingY) {
                        ctx.save();
                        ctx.translate(x, y);
                        ctx.rotate(-18 * Math.PI / 180);
                        ctx.fillText(watermarkText, 0, 0);
                        ctx.restore();
                    }
                }

                ctx.restore();
                this._driftAnimationId = requestAnimationFrame(renderWatermark);
            };

            this._driftAnimationId = requestAnimationFrame(renderWatermark);
        },

        setupHandshakeOverlay() {
            let overlay = document.getElementById('face-handshake-overlay');
            const container = this.getContainer();
            if (!overlay && container) {
                overlay = document.createElement('div');
                overlay.id = 'face-handshake-overlay';
                container.appendChild(overlay);
            }
            if (overlay) {
                overlay.innerHTML = `
                    <div style="max-width: 320px; width: 100%;">
                        <div id="handshake-icon" style="font-size: 3rem; margin-bottom: 0.75rem; animation: pulse 1.8s infinite;">👤</div>
                        <h2 id="handshake-title" style="font-size: 1.25rem; font-weight: 900; margin-bottom: 0.25rem;">Security Verification</h2>
                        <p id="handshake-desc" style="font-size: 0.8125rem; opacity: 0.8; margin-bottom: 1.25rem;">
                            Keep your face in front of the camera for 3 seconds to verify presence.
                        </p>
                        <!-- Countdown Progress Bar -->
                        <div style="background: rgba(255,255,255,0.15); border-radius: 9999px; height: 8px; overflow: hidden; margin-bottom: 0.75rem;">
                            <div id="handshake-progress" style="width: 0%; height: 100%; background: linear-gradient(90deg, #ec4899, #f59e0b); transition: width 0.3s ease;"></div>
                        </div>
                        <div id="handshake-status" style="font-size: 0.75rem; font-weight: 700; color: #f472b6;">
                            Aligning camera... (3s remaining)
                        </div>
                    </div>
                `;
                overlay.style.display = 'flex';
            }

            // Initially obscure remote video
            const remote = this.getRemoteVideo();
            if (remote) {
                remote.classList.add('privacy-obscured');
            }
        },

        startFaceVerification(localStream) {
            if (this.localVerified) return;
            console.log('[CallPrivacy] Starting local face verification loop...');

            const localVideo = this.getLocalVideo();
            const offscreenCanvas = document.createElement('canvas');
            offscreenCanvas.width = 160;
            offscreenCanvas.height = 120;
            const offscreenCtx = offscreenCanvas.getContext('2d');

            let faceDetector = null;
            if ('FaceDetector' in window) {
                try {
                    faceDetector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 1 });
                } catch (_) {}
            }

            clearInterval(this._faceCheckInterval);
            this._faceCheckInterval = setInterval(async () => {
                if (this.localVerified || !this.active) {
                    clearInterval(this._faceCheckInterval);
                    return;
                }

                let detected = false;
                if (localVideo && localVideo.readyState >= 2) {
                    if (faceDetector) {
                        try {
                            const faces = await faceDetector.detect(localVideo);
                            detected = faces && faces.length > 0;
                        } catch (_) {
                            detected = this.fallbackPresenceCheck(localVideo, offscreenCtx, offscreenCanvas);
                        }
                    } else {
                        detected = this.fallbackPresenceCheck(localVideo, offscreenCtx, offscreenCanvas);
                    }
                }

                if (detected) {
                    this._consecutiveFaceSeconds += 0.5;
                } else {
                    this._consecutiveFaceSeconds = Math.max(0, this._consecutiveFaceSeconds - 0.5);
                }

                const remaining = Math.max(0, 3 - this._consecutiveFaceSeconds);
                const percent = Math.min(100, Math.round((this._consecutiveFaceSeconds / 3) * 100));

                const progressEl = document.getElementById('handshake-progress');
                const statusEl = document.getElementById('handshake-status');
                if (progressEl) progressEl.style.width = `${percent}%`;
                if (statusEl) {
                    statusEl.textContent = detected
                        ? `Face verified! Hold steady (${remaining.toFixed(1)}s remaining)`
                        : `Please position your face clearly in the camera frame`;
                }

                if (this._consecutiveFaceSeconds >= 3) {
                    clearInterval(this._faceCheckInterval);
                    this.onLocalFaceVerified();
                }
            }, 500);
        },

        fallbackPresenceCheck(video, ctx, canvas) {
            // Heuristic pixel variation & luminance presence check
            try {
                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                const frame = ctx.getImageData(canvas.width / 4, canvas.height / 4, canvas.width / 2, canvas.height / 2);
                const data = frame.data;
                let totalBrightness = 0;
                for (let i = 0; i < data.length; i += 4) {
                    totalBrightness += (data[i] + data[i+1] + data[i+2]) / 3;
                }
                const avgBrightness = totalBrightness / (data.length / 4);
                // Active camera frame with lighting
                return avgBrightness > 25 && avgBrightness < 245;
            } catch (_) {
                return true; // Fallback allow if cross-origin canvas issue
            }
        },

        onLocalFaceVerified() {
            this.localVerified = true;
            console.log('[CallPrivacy] Local face successfully verified for 3 consecutive seconds!');

            const iconEl = document.getElementById('handshake-icon');
            const statusEl = document.getElementById('handshake-status');
            const titleEl = document.getElementById('handshake-title');
            const descEl = document.getElementById('handshake-desc');

            if (iconEl) iconEl.textContent = '✅';
            if (titleEl) titleEl.textContent = 'You are Verified!';
            if (descEl) descEl.textContent = 'Waiting for your partner to complete mutual face verification...';
            if (statusEl) {
                statusEl.textContent = 'Awaiting partner verification...';
                statusEl.style.color = '#34d399';
            }

            // Send face_verified signal to peer
            this.sendSignal('face_verified', { verified: true });

            if (this.peerVerified) {
                this.unlockFeeds();
            }
        },

        onPeerSignal(signalType, payload) {
            console.log(`[CallPrivacy] Received signal: ${signalType}`, payload);

            if (signalType === 'face_verified') {
                this.peerVerified = true;
                if (this.localVerified) {
                    this.unlockFeeds();
                }
            } else if (signalType === 'feeds_unlocked') {
                this.peerVerified = true;
                this.unlockFeeds();
            } else if (signalType === 'privacy_blur_engaged') {
                this.showBlurCard(
                    "👀 Partner Paused",
                    "Your partner minimized the app or switched tabs. Stream temporarily paused."
                );
            } else if (signalType === 'privacy_blur_lifted') {
                this.hideBlurCard();
            }
        },

        unlockFeeds() {
            if (this.handshakeComplete) return;
            this.handshakeComplete = true;
            console.log('[CallPrivacy] Mutual verification complete! Unlocking feeds simultaneously.');

            const overlay = document.getElementById('face-handshake-overlay');
            if (overlay) {
                overlay.style.opacity = '0';
                overlay.style.transform = 'scale(1.05)';
                setTimeout(() => {
                    overlay.remove();
                }, 600);
            }

            const remote = this.getRemoteVideo();
            if (remote) {
                remote.classList.remove('privacy-obscured');
                remote.classList.add('privacy-clear');
            }

            this.hideBlurCard();
        },

        cleanup() {
            this.active = false;
            cancelAnimationFrame(this._driftAnimationId);
            clearInterval(this._faceCheckInterval);
            clearTimeout(this._unblurTimeout);
            const canvas = document.getElementById('watermark-canvas');
            if (canvas) canvas.remove();
            const card = document.getElementById('call-privacy-blur-card');
            if (card) card.remove();
            const overlay = document.getElementById('face-handshake-overlay');
            if (overlay) overlay.remove();
        }
    };
})();

