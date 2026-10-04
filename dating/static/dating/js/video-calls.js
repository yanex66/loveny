(() => {
    const config = document.getElementById('call-config');
    if (!config) return;

    const incomingSheet = document.getElementById('incoming-call-sheet');
    const incomingName = document.getElementById('incoming-call-title');
    const incomingPhoto = document.getElementById('incoming-caller-photo');
    const roomOverlay = document.getElementById('video-call-room');
    const remoteVideo = document.getElementById('remote-call-video');
    const localVideo = document.getElementById('local-call-video');
    const feedback = document.getElementById('call-feedback');
    const fallbackPhoto = incomingPhoto.src;
    const apiPrefix = config.dataset.callApiPrefix;
    let activeCall = null;
    let peer = null;
    let localStream = null;
    let lastSignalId = 0;
    let signalingTimer = null;
    let statusTimer = null;
    let pendingCandidates = [];
    let cameraFacing = 'user';
    let isCaller = false;
    let incomingRoomId = null;

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

    function toast(message, subscribe = false) {
        const notice = document.createElement('div');
        notice.className = 'fixed left-1/2 top-5 z-[150] w-[min(92vw,28rem)] -translate-x-1/2 rounded-2xl border border-white/70 bg-slate-900 px-4 py-3 text-center text-sm font-semibold text-white shadow-2xl';
        notice.textContent = message;
        if (subscribe) {
            const link = document.createElement('a');
            link.href = config.dataset.subscribeUrl;
            link.className = 'ml-2 underline underline-offset-2';
            link.textContent = 'Get Sex Call Pass';
            notice.append(' ', link);
        }
        document.body.appendChild(notice);
        window.setTimeout(() => notice.remove(), 5500);
    }

    function showIncoming(call) {
        if (activeCall || incomingRoomId === call.room_id) return;
        incomingRoomId = call.room_id;
        incomingName.textContent = `${call.caller_name || 'Someone'} is calling`;
        incomingPhoto.src = call.caller_photo || fallbackPhoto;
        incomingPhoto.onerror = () => { incomingPhoto.onerror = null; incomingPhoto.src = fallbackPhoto; };
        incomingSheet.classList.remove('hidden');
        incomingSheet.classList.add('flex');
    }

    function hideIncoming() {
        incomingRoomId = null;
        incomingSheet.classList.add('hidden');
        incomingSheet.classList.remove('flex');
    }

    function showRoom(message = 'Connecting…') {
        roomOverlay.classList.remove('hidden');
        feedback.textContent = message;
    }

    function stopPolling() {
        window.clearInterval(signalingTimer);
        window.clearInterval(statusTimer);
        signalingTimer = null;
        statusTimer = null;
    }

    function closeRoom() {
        stopPolling();
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
        remoteVideo.srcObject = null;
        localVideo.srcObject = null;
        roomOverlay.classList.add('hidden');
        activeCall = null;
        pendingCandidates = [];
        lastSignalId = 0;
    }

    async function sendSignal(type, payload) {
        if (!activeCall) return;
        try {
            await api(`${apiPrefix}${activeCall.room_id}/signals/send/`, {
                method: 'POST',
                body: JSON.stringify({type, payload}),
            });
        } catch (error) {
            feedback.textContent = error.message;
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
        if (!peer || !activeCall) return;
        try {
            if (signal.type === 'offer' && !isCaller && !peer.remoteDescription) {
                await peer.setRemoteDescription(new RTCSessionDescription(signal.payload));
                await flushCandidates();
                const answer = await peer.createAnswer();
                await peer.setLocalDescription(answer);
                await sendSignal('answer', peer.localDescription.toJSON());
                feedback.textContent = 'Waiting for the other person to connect…';
            } else if (signal.type === 'answer' && isCaller && !peer.remoteDescription) {
                await peer.setRemoteDescription(new RTCSessionDescription(signal.payload));
                await flushCandidates();
                feedback.textContent = 'Call connected';
            } else if (signal.type === 'candidate') {
                const candidate = new RTCIceCandidate(signal.payload);
                if (peer.remoteDescription) await peer.addIceCandidate(candidate);
                else pendingCandidates.push(candidate);
            }
        } catch (error) {
            feedback.textContent = 'Could not establish the video connection.';
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
            feedback.textContent = error.message;
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
            feedback.textContent = error.message;
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
        localVideo.srcObject = localStream;
        peer = new RTCPeerConnection({
            iceServers: [{urls: 'stun:stun.l.google.com:19302'}],
        });
        localStream.getTracks().forEach(track => peer.addTrack(track, localStream));
        peer.onicecandidate = event => {
            if (event.candidate) sendSignal('candidate', event.candidate.toJSON());
        };
        peer.ontrack = event => {
            remoteVideo.srcObject = event.streams[0] || new MediaStream([event.track]);
            remoteVideo.play().catch(() => {});
            feedback.textContent = 'Call connected';
        };
        peer.onconnectionstatechange = () => {
            if (peer && peer.connectionState === 'connected') feedback.textContent = 'Call connected';
            if (peer && ['failed', 'disconnected'].includes(peer.connectionState)) {
                feedback.textContent = 'Connection interrupted. Trying to reconnect…';
            }
        };
    }

    async function initiateCall(receiverId) {
        try {
            const response = await api(config.dataset.initiateUrl, {
                method: 'POST',
                body: JSON.stringify({receiver_id: receiverId}),
            });
            activeCall = response.call;
            isCaller = true;
            showRoom('Starting secure video…');
            await createPeer();
            const offer = await peer.createOffer();
            await peer.setLocalDescription(offer);
            await sendSignal('offer', peer.localDescription.toJSON());
            startPolling();
        } catch (error) {
            closeRoom();
            toast(error.status === 403 && error.payload?.message === 'sex_call_pass_required'
                ? 'A Sex Call Pass or Sex Call mode is required to make video calls.'
                : (error.message || 'Could not start the video call. Check camera and microphone permissions.'),
            error.status === 403 && error.payload?.message === 'sex_call_pass_required');
        }
    }

    async function respondToCall(action) {
        const roomId = incomingRoomId;
        if (!roomId) return;
        hideIncoming();
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
                // The call may already have ended while media permission was requested.
                console.warn('Could not end a call after media setup failed.', cleanupError);
            }
            toast(error.message || 'Could not join the call. Allow camera and microphone access to continue.');
        }
    }

    document.querySelectorAll('[data-video-call-target]').forEach(button => {
        button.addEventListener('click', () => initiateCall(button.dataset.videoCallTarget));
    });
    document.getElementById('accept-call').addEventListener('click', () => respondToCall('accept'));
    document.getElementById('decline-call').addEventListener('click', () => respondToCall('decline'));
    document.getElementById('end-call').addEventListener('click', async () => {
        const roomId = activeCall && activeCall.room_id;
        closeRoom();
        if (roomId) {
            try {
                await api(`${apiPrefix}${roomId}/end/`, {method: 'POST', body: '{}'});
            } catch (error) {
                toast(error.message);
            }
        }
    });

    document.getElementById('mute-call').addEventListener('click', event => {
        if (!localStream) return;
        const enabled = localStream.getAudioTracks().some(track => track.enabled);
        localStream.getAudioTracks().forEach(track => { track.enabled = !enabled; });
        event.currentTarget.textContent = enabled ? 'Unmute' : 'Mute';
        event.currentTarget.setAttribute('aria-label', enabled ? 'Unmute audio' : 'Mute audio');
    });
    document.getElementById('toggle-camera').addEventListener('click', event => {
        if (!localStream) return;
        const enabled = localStream.getVideoTracks().some(track => track.enabled);
        localStream.getVideoTracks().forEach(track => { track.enabled = !enabled; });
        event.currentTarget.textContent = enabled ? 'Cam off' : 'Cam';
        event.currentTarget.setAttribute('aria-label', enabled ? 'Turn camera on' : 'Turn camera off');
    });
    document.getElementById('flip-camera').addEventListener('click', async () => {
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
            localVideo.srcObject = localStream;
        } catch (error) {
            toast('Could not switch cameras. The current camera will stay on.');
            cameraFacing = cameraFacing === 'user' ? 'environment' : 'user';
        }
    });

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

    window.lovenyStartVideoCall = initiateCall;
})();
