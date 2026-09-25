import React, { useState, useEffect, useCallback, useRef } from 'react';
import axios from 'axios';
import ChannelCard from './components/ChannelCard';
import StatsBar from './components/StatsBar';

const API = 'http://localhost:8000/api';

function App() {
  const [channels, setChannels] = useState([]);
  const [channelTotal, setChannelTotal] = useState(0);
  const [channelPage, setChannelPage] = useState(0);
  const CHANNEL_PAGE_SIZE = 200;
  const [acceptedChannels, setAcceptedChannels] = useState([]);
  const [viewingAccepted, setViewingAccepted] = useState(false);
  const [stats, setStats] = useState({
    total_searched: 0, total_found: 0, total_accepted: 0,
    total_rejected: 0, current_batch: 0, total_batches: 0,
    status: 'idle', last_channel: null
  });
  const [logs, setLogs] = useState([]);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [running, setRunning] = useState(false);
  const [paused, setPaused] = useState(false);
  const [uploaded, setUploaded] = useState(true);
  const [loading, setLoading] = useState(false);
  const [forceUrl, setForceUrl] = useState('');
  const [forceSaving, setForceSaving] = useState(false);
  const [showForceSave, setShowForceSave] = useState(false);
  const [sheetStatus, setSheetStatus] = useState({ ids_count: 0, names_count: 0, last_refresh: null });
  const [refreshing, setRefreshing] = useState(false);
  const [emailCheckStats, setEmailCheckStats] = useState({
    status: 'idle', total: 0, completed: 0,
    passed: 0, failed: 0, errors: 0,
    last_result: null, queue_remaining: 0,
    email_checked_count: 0
  });
  const [processingEmails, setProcessingEmails] = useState(false);
  const [recoverStats, setRecoverStats] = useState({
    status: 'idle', total: 0, processed: 0,
    recovered: 0, still_failed: 0, current: null
  });
  const [recovering, setRecovering] = useState(false);
  const [bizScraperRunning, setBizScraperRunning] = useState(false);
  const [aiHop, setAiHop] = useState({ running: false, channels_sent: 0 });
  const [aiEmail, setAiEmail] = useState({ running: false, emails_found: 0 });
  const [dismissedPrompts, setDismissedPrompts] = useState(new Set());
  const [hopTabCount, setHopTabCount] = useState(5);
  const [hopAutoPick, setHopAutoPick] = useState(true);
  const [viewingQueue, setViewingQueue] = useState(false);
  const [queueData, setQueueData] = useState({ total: 0, items: [], keys_exhausted: false });
  const [queueUnavailable, setQueueUnavailable] = useState(false);
  const [queuePersisted, setQueuePersisted] = useState(null);
  const fileInputRef = useRef(null);
  const lastClickedRef = useRef(null);
  const lastAcceptedClickRef = useRef(null);
  const [selectedAcceptedIds, setSelectedAcceptedIds] = useState(new Set());
  const pollInterval = useRef(null);

  // Poll for new channels and stats
  const poll = useCallback(async () => {
    try {
      const [chRes, statsRes, logRes, sheetRes, emailRes, recoverRes, bizRes, aiHopRes, aiEmailRes] = await Promise.all([
        axios.get(`${API}/channels?limit=${CHANNEL_PAGE_SIZE}&offset=${channelPage * CHANNEL_PAGE_SIZE}`),
        axios.get(`${API}/stats`),
        axios.get(`${API}/logs?limit=30`),
        axios.get(`${API}/sheet-status`).catch(() => null),
        axios.get(`${API}/email-check/status`).catch(() => null),
        axios.get(`${API}/recover-rejected/status`).catch(() => null),
        axios.get(`${API}/business-email-scraper/status`).catch(() => null),
        axios.get(`${API}/hop-ai/status`).catch(() => null),
        axios.get(`${API}/email-ai/status`).catch(() => null),
      ]);
      setChannels(chRes.data.channels);
      setChannelTotal(chRes.data.total);
      const lastPage = Math.max(0, Math.ceil(chRes.data.total / CHANNEL_PAGE_SIZE) - 1);
      if (channelPage > lastPage) setChannelPage(lastPage);
      setStats(statsRes.data);
      setLogs(logRes.data.logs);
      setRunning(statsRes.data.status !== 'idle' && statsRes.data.status !== 'stopped');
      setPaused(statsRes.data.status === 'paused');
      if (sheetRes?.data) {
        setSheetStatus(sheetRes.data);
      }
      if (emailRes?.data) {
        setEmailCheckStats(emailRes.data);
      }
      if (recoverRes?.data) {
        setRecoverStats(recoverRes.data);
        setRecovering(recoverRes.data.status === 'running' || recoverRes.data.status === 'ready_to_run');
      }
      if (bizRes?.data) {
        setBizScraperRunning(bizRes.data.running);
      }
      if (aiHopRes?.data) {
        setAiHop(aiHopRes.data);
      }
      if (aiEmailRes?.data) {
        setAiEmail(aiEmailRes.data);
      }
    } catch (e) {
      console.error('Poll error:', e);
    }
  }, [channelPage]);

  useEffect(() => {
    poll();
    pollInterval.current = setInterval(poll, 3000);
    return () => clearInterval(pollInterval.current);
  }, [poll]);

  const fetchAccepted = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/accepted`);
      setAcceptedChannels(res.data.accepted);
    } catch (e) {
      console.error('Fetch accepted error:', e);
    }
  }, []);

  useEffect(() => {
    if (!viewingAccepted) return undefined;
    const t = setInterval(fetchAccepted, 5000);   // keep the Accepted list current while it is open
    return () => clearInterval(t);
  }, [viewingAccepted, fetchAccepted]);

  const fetchQueue = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/queue?limit=300`);
      setQueueData(res.data);
      setQueueUnavailable(false);
    } catch (e) {
      setQueueUnavailable(true);
      console.error('Fetch queue error:', e);
    }
  }, []);

  useEffect(() => {
    // The waiting list is only saved to disk by a backend that has the /api/queue endpoint.
    const check = () => axios.get(`${API}/queue?limit=1`)
      .then(() => setQueuePersisted(true)).catch(() => setQueuePersisted(false));
    check();
    const t = setInterval(check, 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!viewingQueue) return undefined;
    fetchQueue();
    const t = setInterval(fetchQueue, 5000);
    return () => clearInterval(t);
  }, [viewingQueue, fetchQueue]);

  const handleAcceptedClick = () => {
    setViewingQueue(false);
    if (viewingAccepted) {
      setViewingAccepted(false);
    } else {
      fetchAccepted();
      setViewingAccepted(true);
    }
  };

  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    setLoading(true);
    try {
      await axios.post(`${API}/upload-excel`, formData);
      setUploaded(true);
    } catch (e) {
      alert('Upload failed: ' + e.message);
    }
    setLoading(false);
  };

  const startScraper = async () => {
    try {
      await axios.post(`${API}/start`);
      setRunning(true);
    } catch (e) {
      alert('Start failed: ' + e.message);
    }
  };

  const stopScraper = async () => {
    await axios.post(`${API}/stop`);
    setRunning(false);
  };

  const pauseScraper = async () => {
    await axios.post(`${API}/pause`);
    setPaused(true);
  };

  const resumeScraper = async () => {
    await axios.post(`${API}/resume`);
    setPaused(false);
  };

  const toggleSelect = (id) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Click one card, then shift+click another: everything in between gets selected too.
  const handleCardClick = (id, e) => {
    if (e && e.shiftKey && lastClickedRef.current && lastClickedRef.current !== id) {
      const ids = channels.map(c => c.id);
      const a = ids.indexOf(lastClickedRef.current);
      const b = ids.indexOf(id);
      if (a !== -1 && b !== -1) {
        const [from, to] = a < b ? [a, b] : [b, a];
        setSelectedIds(prev => {
          const next = new Set(prev);
          ids.slice(from, to + 1).forEach(x => next.add(x));
          return next;
        });
        lastClickedRef.current = id;
        return;
      }
    }
    lastClickedRef.current = id;
    toggleSelect(id);
  };

  const handleAcceptedCardClick = (id, e) => {
    const ids = acceptedChannels.map(c => c.id);
    if (e && e.shiftKey && lastAcceptedClickRef.current && lastAcceptedClickRef.current !== id) {
      const a = ids.indexOf(lastAcceptedClickRef.current);
      const b = ids.indexOf(id);
      if (a !== -1 && b !== -1) {
        const [from, to] = a < b ? [a, b] : [b, a];
        setSelectedAcceptedIds(prev => {
          const next = new Set(prev);
          ids.slice(from, to + 1).forEach(x => next.add(x));
          return next;
        });
        lastAcceptedClickRef.current = id;
        return;
      }
    }
    lastAcceptedClickRef.current = id;
    setSelectedAcceptedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const sendAcceptedToEmailCheck = async () => {
    const ids = Array.from(selectedAcceptedIds);
    if (ids.length === 0) return;
    try {
      const res = await axios.post(`${API}/email-check/queue`, { channel_ids: ids });
      setSelectedAcceptedIds(new Set());
      alert(`${res.data.queued} channel(s) sent to email check` +
            (res.data.already_queued ? ` (${res.data.already_queued} were already queued)` : ''));
      poll();
      fetchAccepted();
    } catch (e) {
      alert('Failed to queue: ' + e.message);
    }
  };

  const selectAll = async () => {
    const res = await axios.get(`${API}/channels?limit=1000000&offset=0`);
    const allIds = res.data.channels.map(c => c.id);
    setSelectedIds(new Set(allIds));
    await axios.post(`${API}/channels/select-all`, { select: true });
  };

  const deselectAll = async () => {
    setSelectedIds(new Set());
    await axios.post(`${API}/channels/select-all`, { select: false });
  };

  const acceptSelected = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    await axios.post(`${API}/channels/action`, { channel_ids: ids, action: 'accept' });
    setSelectedIds(new Set());
    poll();
  };

  const rejectSelected = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    await axios.post(`${API}/channels/action`, { channel_ids: ids, action: 'reject' });
    setSelectedIds(new Set());
    poll();
  };

  const handleRefreshSheet = async () => {
    setRefreshing(true);
    try {
      const res = await axios.post(`${API}/refresh-sheet`);
      if (res.data.status === 'ok') {
        setSheetStatus(prev => ({
          ...prev,
          ids_count: res.data.ids || 0,
          names_count: res.data.names || 0,
          last_refresh: new Date().toISOString()
        }));
      }
      poll();
    } catch (e) {
      console.error('Sheet refresh error:', e);
    }
    setRefreshing(false);
  };

  const handleProcessAccepted = async () => {
    setProcessingEmails(true);
    try {
      const res = await axios.post(`${API}/email-check/process-accepted`);
      if (res.data.queued > 0) {
        poll();
      } else {
        alert(res.data.message);
      }
    } catch (e) {
      console.error('Process accepted error:', e);
      alert('Error: ' + (e.response?.data?.error || e.message));
    }
    setProcessingEmails(false);
  };

  const handleStartRecover = async () => {
    try {
      const res = await axios.post(`${API}/recover-rejected/start`);
      if (res.data.status === 'ok') {
        setRecovering(true);
      } else {
        alert(res.data.message);
      }
    } catch (e) {
      console.error('Recover error:', e);
    }
  };

  const handleStartBusinessEmailScraper = async () => {
    try {
      const res = await axios.post(`${API}/business-email-scraper/start`);
      if (res.data.status === 'already_running') {
        alert('Already running — check the open Chrome window.');
      } else {
        setBizScraperRunning(true);
      }
    } catch (e) {
      console.error('Business email scraper start error:', e);
      alert('Start failed: ' + (e.response?.data?.error || e.message));
    }
  };

  const handleStartAiHop = async () => {
    try {
      const res = await axios.post(`${API}/hop-ai/start?tabs=${hopTabCount}&autopick=${hopAutoPick}`);
      if (res.data.status === 'already_running') {
        alert('Hop with AI is already running — check the open Chrome window.');
      } else {
        setAiHop((prev) => ({ ...prev, running: true }));
      }
    } catch (e) {
      console.error('Hop AI start error:', e);
      alert('Start failed: ' + (e.response?.data?.error || e.message));
    }
  };

  const handleStopAiHop = async () => {
    try {
      await axios.post(`${API}/hop-ai/stop`);
      setAiHop((prev) => ({ ...prev, running: false }));
      poll();
    } catch (e) {
      console.error('Hop AI stop error:', e);
    }
  };

  const dismissPrompt = (id) => setDismissedPrompts(prev => new Set(prev).add(id));

  const handleHopPick = async (tab, video) => {
    dismissPrompt(tab.prompt_id);
    try {
      await axios.post(`${API}/hop-ai/pick`, {
        tab: tab.tab, prompt_id: tab.prompt_id, video_id: video.video_id,
      });
      poll();
    } catch (e) {
      alert('Pick failed: ' + e.message);
    }
  };

  const handleHopSkip = async (tab) => {
    dismissPrompt(tab.prompt_id);
    try {
      await axios.post(`${API}/hop-ai/skip`, { tab: tab.tab });
      poll();
    } catch (e) {
      alert('Skip failed: ' + e.message);
    }
  };

  const handleNextBatch = async () => {
    try {
      await axios.post(`${API}/hop-ai/next-batch`);
      poll();
    } catch (e) {
      alert('Next batch failed: ' + e.message);
    }
  };

  const fmtViews = (n) => n == null ? '' :
    n >= 1e6 ? (n / 1e6).toFixed(1) + 'M views' : n >= 1e3 ? Math.round(n / 1e3) + 'K views' : n + ' views';
  const fmtDur = (s) => s == null ? '' :
    (s >= 3600 ? Math.floor(s / 3600) + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0')
               : Math.floor(s / 60)) + ':' + String(s % 60).padStart(2, '0');

  const hopTabs = aiHop.running ? (aiHop.tabs || []) : [];
  const waitingTabs = hopTabs.filter(t => t.prompt_id && (t.phase === 'choosing' || t.phase === 'dead_end'));
  const pickerTab = waitingTabs.find(t => !dismissedPrompts.has(t.prompt_id));
  const hiddenWaiting = waitingTabs.filter(t => dismissedPrompts.has(t.prompt_id));

  const handleStartAiEmail = async () => {
    try {
      const res = await axios.post(`${API}/email-ai/start`);
      if (res.data.status === 'already_running') {
        alert('Email Scraper with AI is already running — check the open Chrome window.');
      } else {
        setAiEmail((prev) => ({ ...prev, running: true }));
      }
    } catch (e) {
      console.error('Email AI start error:', e);
      alert('Start failed: ' + (e.response?.data?.error || e.message));
    }
  };

  const handleStopAiEmail = async () => {
    try {
      await axios.post(`${API}/email-ai/stop`);
      setAiEmail((prev) => ({ ...prev, running: false }));
      poll();
    } catch (e) {
      console.error('Email AI stop error:', e);
    }
  };

  const handleResetRecover = async () => {
    if (!window.confirm('Reset recovery? Every previously-checked rejected channel becomes eligible to be re-checked again.')) return;
    try {
      const res = await axios.post(`${API}/recover-rejected/reset`);
      alert(res.data.message);
      poll();
    } catch (e) {
      console.error('Reset recover error:', e);
      alert('Reset failed: ' + (e.response?.data?.message || e.message));
    }
  };

  const handleForceSave = async () => {
    const url = forceUrl.trim();
    if (!url) return;
    setForceSaving(true);
    try {
      const res = await axios.post(`${API}/channels/force-save`, { url });
      if (res.data.error) {
        alert('❌ ' + res.data.error);
      } else {
        alert('✅ ' + res.data.message + ': ' + res.data.name);
        setForceUrl('');
        setShowForceSave(false);
        if (viewingAccepted) fetchAccepted();
        poll();
      }
    } catch (e) {
      alert('Force save failed: ' + (e.response?.data?.error || e.message));
    }
    setForceSaving(false);
  };

  const exportCSV = async () => {
    window.open(`${API}/export/csv`, '_blank');
  };

  const exportExcel = async () => {
    window.open(`${API}/export/excel`, '_blank');
  };

  const selectedCount = selectedIds.size;

  // Email check display helpers
  const ecStatus = emailCheckStats.status || 'idle';
  const isEmailRunning = ecStatus === 'running' || ecStatus === 'queued';
  const ecTotalChecked = emailCheckStats.passed + emailCheckStats.failed + emailCheckStats.errors;

  return (
    <div className="app">
      <div className="header">
        <h1>🔍 YouTube Channel <span>Discovery</span></h1>
        <div style={{ display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ color: '#22c55e', fontSize: '14px' }}>
            ✅ Google Sheets connected
          </span>
          <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
            {sheetStatus.ids_count > 0
              ? `${sheetStatus.ids_count} known channels`
              : 'Loading...'}
          </span>
          <button
            onClick={handleRefreshSheet}
            disabled={refreshing}
            style={{
              background: refreshing ? '#3f3f46' : '#18181b',
              color: '#e4e4e7',
              border: '1px solid #3f3f46',
              borderRadius: '6px',
              padding: '4px 12px',
              fontSize: '12px',
              cursor: refreshing ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px'
            }}
            title="Refresh dedup data from Google Sheets"
          >
            {refreshing ? '⏳' : '🔄'} Refresh Sheet
          </button>
        </div>
      </div>

      <StatsBar stats={stats} onAcceptedClick={handleAcceptedClick} viewingAccepted={viewingAccepted} />

      {/* ── Email Verification Panel ── */}
      <div className="control-panel" style={{
        borderColor: isEmailRunning ? '#a855f7' : (emailCheckStats.passed > 0 ? '#22c55e' : '#3f3f46'),
        marginBottom: '12px'
      }}>
        <div className="control-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, fontSize: '14px', color: '#e4e4e7' }}>
              ✉️ Email Verification
            </span>

            {/* Status badges */}
            {isEmailRunning && (
              <span style={{
                background: '#a855f7', color: '#fff', padding: '2px 10px',
                borderRadius: '4px', fontSize: '11px', fontWeight: 700
              }}>
                ⏳ Processing ({emailCheckStats.completed}/{emailCheckStats.total})
              </span>
            )}

            {emailCheckStats.completed > 0 && (
              <>
                {emailCheckStats.passed > 0 && (
                  <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                    ✅ {emailCheckStats.passed} saved
                  </span>
                )}
                {emailCheckStats.failed > 0 && (
                  <span style={{ color: '#ef4444', fontSize: '12px', fontWeight: 600 }}>
                    ❌ {emailCheckStats.failed} no email
                  </span>
                )}
                {emailCheckStats.errors > 0 && (
                  <span style={{ color: '#f59e0b', fontSize: '12px', fontWeight: 600 }}>
                    ⚠️ {emailCheckStats.errors} errors
                  </span>
                )}
              </>
            )}

            {!isEmailRunning && emailCheckStats.completed === 0 && emailCheckStats.email_checked_count > 0 && (
              <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
                {emailCheckStats.email_checked_count} channels already verified
              </span>
            )}
          </div>

          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
              {stats.total_accepted} accepted
            </span>

            <button
              onClick={handleProcessAccepted}
              disabled={processingEmails || isEmailRunning || stats.total_accepted === 0}
              style={{
                background: processingEmails || isEmailRunning ? '#3f3f46' : '#7c3aed',
                color: '#fff',
                border: 'none',
                borderRadius: '6px',
                padding: '6px 14px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: (processingEmails || isEmailRunning || stats.total_accepted === 0) ? 'not-allowed' : 'pointer',
                whiteSpace: 'nowrap',
              }}
              title="Check all accepted channels for business email button"
            >
              {processingEmails ? '⏳' : isEmailRunning ? '⏳' : '📧'} {processingEmails || isEmailRunning ? 'Processing...' : 'Verify Emails'}
            </button>
          </div>
        </div>

        {/* Last result notification */}
        {emailCheckStats.last_result && (
          <div style={{
            marginTop: '8px',
            padding: '6px 12px',
            borderRadius: '6px',
            fontSize: '12px',
            background: emailCheckStats.last_result.result === 'passed' ? '#052e16' :
                        emailCheckStats.last_result.result === 'failed' ? '#2d0a0a' : '#1c1917',
            color: emailCheckStats.last_result.result === 'passed' ? '#22c55e' :
                   emailCheckStats.last_result.result === 'failed' ? '#ef4444' : '#f59e0b',
            borderLeft: `3px solid ${
              emailCheckStats.last_result.result === 'passed' ? '#22c55e' :
              emailCheckStats.last_result.result === 'failed' ? '#ef4444' : '#f59e0b'
            }`
          }}>
            <strong>Latest:</strong> {emailCheckStats.last_result.name} → {emailCheckStats.last_result.message}
          </div>
        )}
      </div>

      {/* ── Hop picker popup ── */}
      {pickerTab && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)', zIndex: 1000,
          display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px',
        }}>
          <div style={{
            background: '#0f0f13', border: '1px solid #06b6d4', borderRadius: '10px',
            width: 'min(1100px, 100%)', maxHeight: '90vh', overflowY: 'auto', padding: '16px',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', marginBottom: '12px', flexWrap: 'wrap' }}>
              <div style={{ color: '#e4e4e7', fontWeight: 700, fontSize: '15px' }}>
                🤖 Tab {pickerTab.tab + 1} · "{pickerTab.keyword}" · Hop {pickerTab.hop || 0} — pick the next video
                {waitingTabs.length > 1 && <span style={{ color: '#f59e0b', fontSize: '12px' }}> ({waitingTabs.length} tabs waiting)</span>}
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button className="btn btn-secondary" onClick={() => handleHopSkip(pickerTab)}>
                  ⏭️ Skip this keyword
                </button>
                <button className="btn btn-secondary" onClick={() => dismissPrompt(pickerTab.prompt_id)}>
                  ✕ Hide
                </button>
              </div>
            </div>

            {pickerTab.phase === 'dead_end' || (pickerTab.suggestions || []).length === 0 ? (
              <div style={{ color: '#a1a1aa', padding: '24px', textAlign: 'center' }}>
                Nothing relevant left on this page. Hide this and click any video yourself in Chrome — it carries on from there — or skip the keyword.
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '12px' }}>
                {pickerTab.suggestions.map(v => (
                  <div
                    key={v.video_id}
                    onClick={() => handleHopPick(pickerTab, v)}
                    style={{
                      background: '#18181b', border: '1px solid #3f3f46', borderRadius: '8px',
                      cursor: 'pointer', overflow: 'hidden',
                    }}
                    onMouseEnter={e => e.currentTarget.style.borderColor = '#06b6d4'}
                    onMouseLeave={e => e.currentTarget.style.borderColor = '#3f3f46'}
                  >
                    <div style={{ position: 'relative' }}>
                      <img src={v.thumb} alt="" style={{ width: '100%', display: 'block', aspectRatio: '16 / 9', objectFit: 'cover' }} />
                      {v.dur != null && (
                        <span style={{
                          position: 'absolute', right: 6, bottom: 6, background: 'rgba(0,0,0,0.8)',
                          color: '#fff', fontSize: '11px', padding: '1px 5px', borderRadius: '3px',
                        }}>{fmtDur(v.dur)}</span>
                      )}
                    </div>
                    <div style={{ padding: '8px 10px' }}>
                      <div style={{ color: '#e4e4e7', fontSize: '13px', fontWeight: 600, lineHeight: 1.3 }}>{v.title}</div>
                      <div style={{ color: '#a1a1aa', fontSize: '12px', marginTop: '4px' }}>
                        {v.channel}{v.views != null && <> · {fmtViews(v.views)}</>}
                      </div>
                      <div style={{ marginTop: '6px', display: 'flex', gap: '6px', flexWrap: 'wrap', fontSize: '10px' }}>
                        {v.real_face && <span style={{ background: '#052e16', color: '#22c55e', padding: '1px 6px', borderRadius: '3px' }}>👤 real person</span>}
                        {!v.real_face && v.face && <span style={{ background: '#1c1917', color: '#a1a1aa', padding: '1px 6px', borderRadius: '3px' }}>face</span>}
                        {v.mono > 0 && <span style={{ background: '#2e1065', color: '#c4b5fd', padding: '1px 6px', borderRadius: '3px' }}>💰 monetizing</span>}
                        <span style={{ background: '#082f3a', color: '#22d3ee', padding: '1px 6px', borderRadius: '3px' }}>niche {v.niche}</span>
                        <span style={{ background: '#27272a', color: '#a1a1aa', padding: '1px 6px', borderRadius: '3px' }}>score {v.score}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Hop with AI Panel ── */}
      <div className="control-panel" style={{
        borderColor: aiHop.running ? '#06b6d4' : (aiHop.channels_sent > 0 ? '#22c55e' : '#3f3f46'),
        marginBottom: '12px'
      }}>
        <div className="control-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, fontSize: '14px', color: '#e4e4e7' }}>
              🤖 Hop with AI
            </span>

            {aiHop.running && (
              <span style={{
                background: '#06b6d4', color: '#000', padding: '2px 10px',
                borderRadius: '4px', fontSize: '11px', fontWeight: 700
              }}>
                ⏳ Batch {aiHop.batch || 1} · Keyword {(aiHop.keyword_idx ?? 0) + 1}
                {(aiHop.batch_size || 1) > 1 ? `–${(aiHop.keyword_idx ?? 0) + (aiHop.batch_size || 1)}` : ''}
                /{aiHop.total_keywords || '?'}
              </span>
            )}

            {waitingTabs.length > 0 && (
              <span style={{ color: '#f59e0b', fontSize: '12px', fontWeight: 700 }}>
                👆 {waitingTabs.length} waiting for your pick
              </span>
            )}

            {aiHop.channels_sent > 0 && (
              <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                ✅ {aiHop.channels_sent} channels harvested
              </span>
            )}

            {!aiHop.running && aiHop.phase === 'finished' && (
              <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                🏁 All keywords processed
              </span>
            )}

            {!aiHop.running && stats.queue_size > 0 && (
              <span style={{ color: '#f59e0b', fontSize: '12px', fontWeight: 600 }}>
                ⚙️ {stats.queue_size} still validating in background
              </span>
            )}
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            {!aiHop.running ? (
              <>
              <select
                value={hopTabCount}
                onChange={e => setHopTabCount(Number(e.target.value))}
                style={{
                  background: '#18181b', color: '#e4e4e7', border: '1px solid #3f3f46',
                  borderRadius: '6px', padding: '6px 8px', fontSize: '12px',
                }}
                title="How many Chrome tabs to run in parallel, each on its own keyword"
              >
                {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(n => (
                  <option key={n} value={n}>{n} tab{n > 1 ? 's' : ''}</option>
                ))}
              </select>
              <label style={{ color: '#a1a1aa', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}
                title="Let the AI (Groq) choose the next video itself. When a page has nothing relevant it stops and waits for you.">
                <input type="checkbox" checked={hopAutoPick} onChange={e => setHopAutoPick(e.target.checked)} />
                AI auto-pick
              </label>
              <button
                onClick={handleStartAiHop}
                style={{
                  background: '#06b6d4', color: '#000', border: 'none',
                  borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                  fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                }}
                title="Scrolls each results/recommendation page fully, harvests channels, then shows you the 10-15 most relevant videos to hop to"
              >
                🤖 Hop with AI
              </button>
              </>
            ) : (
              <>
                {hiddenWaiting.length > 0 && (
                  <button
                    onClick={() => setDismissedPrompts(new Set())}
                    style={{
                      background: '#f59e0b', color: '#000', border: 'none',
                      borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                      fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                    }}
                  >
                    👆 Open picker ({hiddenWaiting.length})
                  </button>
                )}
                <button
                  onClick={handleNextBatch}
                  style={{
                    background: '#22c55e', color: '#052e16', border: 'none',
                    borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                    fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                  }}
                  title="Finish the current keywords and start the next batch of keywords"
                >
                  ⏭️ Next Batch
                </button>
                <button
                  onClick={handleStopAiHop}
                  style={{
                    background: '#ef4444', color: '#fff', border: 'none',
                    borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                    fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                  }}
                  title="Stop the AI hopper (progress is saved after every hop)"
                >
                  ⏹️ Stop AI Hop
                </button>
              </>
            )}
          </div>
        </div>

        {/* Per-tab status */}
        {hopTabs.map(t => (
          <div key={t.tab} style={{
            marginTop: '8px', padding: '6px 12px', borderRadius: '6px',
            fontSize: '12px', background: '#082f3a', color: '#22d3ee',
            borderLeft: '3px solid #06b6d4'
          }}>
            <strong>Tab {t.tab + 1}</strong>
            {t.keyword && <> · "{t.keyword}"</>}
            {' · '}Hop {t.hop || 0}
            {' · '}<span style={{ opacity: 0.85 }}>
              {t.phase === 'scrolling' ? `scrolling… ${t.items || 0} videos loaded` : (t.message || t.phase)}
            </span>
            {t.current && <><br /><strong>Watching:</strong> {t.current.title}
              {t.current.channel && <> · <em>{t.current.channel}</em></>}</>}
          </div>
        ))}

        {aiHop.phase === 'error' && aiHop.message && (
          <div style={{
            marginTop: '8px', padding: '6px 12px', borderRadius: '6px',
            fontSize: '12px', background: '#2d0a0a', color: '#ef4444',
            borderLeft: '3px solid #ef4444'
          }}>
            {aiHop.message}
          </div>
        )}
      </div>

      {/* ── Email Scraper with AI Panel ── */}
      <div className="control-panel" style={{
        borderColor: aiEmail.running ? '#a855f7' : (aiEmail.emails_found > 0 ? '#22c55e' : '#3f3f46'),
        marginBottom: '12px'
      }}>
        <div className="control-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, fontSize: '14px', color: '#e4e4e7' }}>
              ✉️ Email Scraper with AI
            </span>

            {aiEmail.running && (
              <span style={{
                background: '#a855f7', color: '#fff', padding: '2px 10px',
                borderRadius: '4px', fontSize: '11px', fontWeight: 700
              }}>
                ⏳ Row {aiEmail.row || 0}/{aiEmail.total_rows || '?'} · Account {(aiEmail.account_idx ?? 0) + 1}/{aiEmail.total_accounts || '?'}
              </span>
            )}

            {aiEmail.running && aiEmail.account && (
              <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
                {aiEmail.account}
              </span>
            )}

            {aiEmail.emails_found > 0 && (
              <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                ✅ {aiEmail.emails_found} emails saved
              </span>
            )}

            {!aiEmail.running && aiEmail.phase === 'finished' && (
              <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                🏁 {aiEmail.message || 'Finished'}
              </span>
            )}
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            {!aiEmail.running ? (
              <button
                onClick={handleStartAiEmail}
                style={{
                  background: '#a855f7', color: '#fff', border: 'none',
                  borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                  fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                }}
                title="Fully automatic: opens each channel's About page, clicks View email address, waits for CapSolver's tick, clicks Submit, saves the email to the sheet, and switches Gmail account on rate limit"
              >
                ✉️ Email Scraper with AI
              </button>
            ) : (
              <button
                onClick={handleStopAiEmail}
                style={{
                  background: '#ef4444', color: '#fff', border: 'none',
                  borderRadius: '6px', padding: '6px 14px', fontSize: '12px',
                  fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
                }}
                title="Stop the AI email scraper (every found email is already saved to the sheet)"
              >
                ⏹️ Stop AI Email
              </button>
            )}
          </div>
        </div>

        {/* Currently processing */}
        {aiEmail.running && aiEmail.last_channel && (
          <div style={{
            marginTop: '8px', padding: '6px 12px', borderRadius: '6px',
            fontSize: '12px', background: '#2a0a3a', color: '#d8b4fe',
            borderLeft: '3px solid #a855f7'
          }}>
            <strong>Processing:</strong> {aiEmail.last_channel}
            {aiEmail.message && <> · <span style={{ opacity: 0.7 }}>{aiEmail.message}</span></>}
            {aiEmail.last_email && <> · <span style={{ color: '#22c55e' }}>last: {aiEmail.last_email}</span></>}
          </div>
        )}

        {aiEmail.phase === 'error' && aiEmail.message && (
          <div style={{
            marginTop: '8px', padding: '6px 12px', borderRadius: '6px',
            fontSize: '12px', background: '#2d0a0a', color: '#ef4444',
            borderLeft: '3px solid #ef4444'
          }}>
            {aiEmail.message}
          </div>
        )}
      </div>

      {/* ── Recovery Panel ── */}
      <div className="control-panel" style={{
        borderColor: recovering ? '#f59e0b' : (recoverStats.recovered > 0 ? '#22c55e' : '#3f3f46'),
        marginBottom: '12px'
      }}>
        <div className="control-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, fontSize: '14px', color: '#e4e4e7' }}>
              🔄 Recover Rejected Channels
            </span>

            {recovering && (
              <span style={{
                background: '#f59e0b', color: '#000', padding: '2px 10px',
                borderRadius: '4px', fontSize: '11px', fontWeight: 700
              }}>
                ⏳ Processing ({recoverStats.processed}/{recoverStats.total})
              </span>
            )}

            {!recovering && recoverStats.recovered > 0 && (
              <span style={{ color: '#22c55e', fontSize: '12px', fontWeight: 600 }}>
                ✅ {recoverStats.recovered} recovered last run
              </span>
            )}

            {!recovering && recoverStats.total > 0 && (
              <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
                {recoverStats.recovered}/{recoverStats.total} recovered
              </span>
            )}

            {!recovering && recoverStats.checked_ever > 0 && (
              <span style={{ color: '#a1a1aa', fontSize: '12px' }}>
                {recoverStats.checked_ever} already checked · {recoverStats.remaining_to_check} remaining
              </span>
            )}
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleStartRecover}
              disabled={recovering}
              style={{
                background: recovering ? '#3f3f46' : '#f59e0b',
                color: recovering ? '#a1a1aa' : '#000',
                border: 'none',
                borderRadius: '6px',
                padding: '6px 14px',
                fontSize: '12px',
                fontWeight: 700,
                cursor: recovering ? 'not-allowed' : 'pointer',
                whiteSpace: 'nowrap',
              }}
              title="Re-check rejected channels not yet checked by a recovery run"
            >
              {recovering ? '⏳ Running...' : '🔄 Recover Rejected'}
            </button>

            <button
              onClick={handleResetRecover}
              disabled={recovering}
              style={{
                background: 'transparent',
                color: recovering ? '#3f3f46' : '#a1a1aa',
                border: '1px solid #3f3f46',
                borderRadius: '6px',
                padding: '6px 14px',
                fontSize: '12px',
                fontWeight: 700,
                cursor: recovering ? 'not-allowed' : 'pointer',
                whiteSpace: 'nowrap',
              }}
              title="Clear recovery history so every rejected channel can be re-checked from scratch"
            >
              ↺ Reset
            </button>
          </div>
        </div>

        {/* Live recovery progress */}
        {recovering && recoverStats.current && (
          <div style={{
            marginTop: '8px',
            padding: '6px 12px',
            borderRadius: '6px',
            fontSize: '12px',
            background: '#1c1917',
            color: '#f59e0b',
            borderLeft: '3px solid #f59e0b'
          }}>
            <strong>#{recoverStats.current.index}/{recoverStats.current.total}</strong>
            {recoverStats.current.name && <> · <em>"{recoverStats.current.name}"</em></>}
            {recoverStats.current.status && <> · <span style={{opacity:0.7}}>{recoverStats.current.status}</span></>}
            <br />
            <strong style={{ color: '#22c55e' }}> ✅ {recoverStats.recovered} recovered</strong> ·
            <strong style={{ color: '#ef4444' }}> ❌ {recoverStats.still_failed} failed</strong>
          </div>
        )}

        {/* Summary when done */}
        {!recovering && recoverStats.total > 0 && (
          <div style={{
            marginTop: '8px',
            padding: '6px 12px',
            borderRadius: '6px',
            fontSize: '12px',
            background: recoverStats.recovered > 0 ? '#052e16' : '#1c1917',
            color: recoverStats.recovered > 0 ? '#22c55e' : '#a1a1aa',
            borderLeft: `3px solid ${recoverStats.recovered > 0 ? '#22c55e' : '#3f3f46'}`
          }}>
            <strong>Done.</strong> Checked {recoverStats.total}, recovered {recoverStats.recovered}, {recoverStats.still_failed} still failed
          </div>
        )}
      </div>

      <div className="control-panel">
        <div className="control-row">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileUpload}
            accept=".xlsx"
            className="file-input"
            id="file-upload"
          />
          <label htmlFor="file-upload" className="file-label">
            📁 Replace Leads_Master.xlsx
          </label>

          <button
            className="btn btn-primary"
            onClick={startScraper}
            disabled={running || loading}
          >
            {running ? '⏳ Running...' : '▶️ Start Scraping'}
          </button>

          {running && !paused && (
            <button className="btn btn-warning" onClick={pauseScraper}>
              ⏸️ Pause
            </button>
          )}

          {paused && (
            <button className="btn btn-primary" onClick={resumeScraper}>
              ▶️ Resume
            </button>
          )}

          {running && (
            <button className="btn btn-danger" onClick={stopScraper}>
              ⏹️ Stop
            </button>
          )}

          <button className="btn btn-success" onClick={async () => {
            await axios.post(`${API}/keyword/done`);
            poll();
          }} style={{ background: '#22c55e', color: '#052e16', fontWeight: 700 }}>
            ✅ Done — Next Keyword
          </button>

          <button className="btn btn-secondary" onClick={exportCSV}>
            📥 Export CSV
          </button>
          <button className="btn btn-secondary" onClick={exportExcel}>
            📥 Export Excel
          </button>

          <button className="btn btn-secondary" onClick={() => setShowForceSave(!showForceSave)}
            style={showForceSave ? { background: '#22c55e', color: '#052e16', border: '1px solid #22c55e' } : {}}>
            ⚡ Force Save
          </button>

          <button
            className="btn btn-primary"
            onClick={handleStartBusinessEmailScraper}
            disabled={bizScraperRunning}
            title="Opens Chrome, cycles through the Gmail profiles, and grabs business emails for the '5. Needs Email' sheet"
          >
            {bizScraperRunning ? '⏳ Scraper Running...' : '📧 Start Business Email Scraper'}
          </button>
        </div>
        {showForceSave && (
          <div style={{ display: 'flex', gap: '10px', marginTop: '12px', alignItems: 'center' }}>
            <input
              type="text"
              value={forceUrl}
              onChange={(e) => setForceUrl(e.target.value)}
              placeholder="Paste YouTube URL, @handle, or channel ID..."
              style={{
                flex: 1, padding: '10px 14px', borderRadius: '8px', border: '1px solid #3f3f46',
                background: '#0a0a0f', color: '#e4e4e7', fontSize: '14px', outline: 'none'
              }}
              onKeyDown={(e) => { if (e.key === 'Enter') handleForceSave(); }}
            />
            <button
              className="btn btn-accept"
              onClick={handleForceSave}
              disabled={forceSaving || !forceUrl.trim()}
              style={{ padding: '10px 24px', fontWeight: 700, whiteSpace: 'nowrap' }}
            >
              {forceSaving ? '⏳ Saving...' : '💾 Save Directly'}
            </button>
          </div>
        )}
      </div>

      {/* View toggle indicator */}
      {viewingAccepted && (
        <div className="control-panel" style={{ marginBottom: '16px', background: '#052e16', borderColor: '#22c55e' }}>
          <div className="control-row" style={{ justifyContent: 'space-between' }}>
            <span style={{ color: '#22c55e', fontWeight: 600, fontSize: '16px' }}>
              ✅ Saved Channels ({acceptedChannels.length} total)
            </span>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              <button className="btn btn-secondary"
                onClick={() => setSelectedAcceptedIds(new Set(acceptedChannels.map(c => c.id)))}>
                ☑️ Select All ({acceptedChannels.length})
              </button>
              <button className="btn btn-secondary" onClick={() => setSelectedAcceptedIds(new Set())}>
                ☐ Deselect All
              </button>
              {selectedAcceptedIds.size > 0 && (
                <button className="btn btn-accept" onClick={sendAcceptedToEmailCheck}>
                  📧 Send {selectedAcceptedIds.size} back to email check
                </button>
              )}
              <button className="btn btn-secondary" onClick={() => setViewingAccepted(false)}>
                ← Back to Pending
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Waiting queue: channels found but not yet validated (kept on disk across restarts) */}
      {(stats.queue_size > 0 || viewingQueue) && (
        <div className="control-panel" style={{ marginBottom: '16px', borderColor: '#f59e0b' }}>
          <div className="control-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
            <span style={{ color: '#f59e0b', fontWeight: 600 }}>
              ⏳ {stats.queue_size} channel{stats.queue_size === 1 ? '' : 's'} waiting to be checked
              {stats.status === 'keys_exhausted' && ' — all API keys are used up, they will resume when quota resets'}
              {queuePersisted === true && (
                <span style={{ color: '#a1a1aa', fontWeight: 400, fontSize: '12px' }}> · saved to disk, safe across restarts</span>
              )}
              {queuePersisted === false && (
                <span style={{ color: '#ef4444', fontWeight: 600, fontSize: '12px' }}> · ⚠ only in memory - do NOT restart the backend yet</span>
              )}
            </span>
            <button className="btn btn-secondary" onClick={() => { setViewingAccepted(false); setViewingQueue(v => !v); }}>
              {viewingQueue ? '← Back to Pending' : '📋 View waiting queue'}
            </button>
          </div>
          {viewingQueue && (
            <div style={{ marginTop: '12px', maxHeight: '420px', overflowY: 'auto', fontSize: '12px' }}>
              <div style={{ color: '#a1a1aa', marginBottom: '6px' }}>
                {queueUnavailable
                  ? 'The list is not available yet - the backend needs one restart to load this feature (the queue count above is live).'
                  : `Showing the first ${queueData.items.length} of ${queueData.total}`}
              </div>
              {queueData.items.map((it, n) => (
                <div key={n} style={{ padding: '4px 0', borderBottom: '1px solid #27272a', color: '#e4e4e7' }}>
                  <span style={{ color: '#71717a' }}>{n + 1}.</span>{' '}
                  {it.url
                    ? <a href={it.url} target="_blank" rel="noreferrer" style={{ color: '#22d3ee' }}>{it.name || it.id || it.url}</a>
                    : (it.name || it.id || (it.video_id && `video ${it.video_id}`) || 'unknown')}
                  <span style={{ color: '#71717a' }}> · {it.source || 'hop'}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Pagination — only for pending view, only when there's more than one page */}
      {!viewingAccepted && !viewingQueue && channelTotal > CHANNEL_PAGE_SIZE && (
        <div className="control-panel" style={{ marginBottom: '16px' }}>
          <div className="control-row" style={{ justifyContent: 'space-between' }}>
            <span>
              Showing {channelPage * CHANNEL_PAGE_SIZE + 1}
              –{Math.min((channelPage + 1) * CHANNEL_PAGE_SIZE, channelTotal)} of {channelTotal}
            </span>
            <div>
              <button
                className="btn btn-secondary"
                disabled={channelPage === 0}
                onClick={() => setChannelPage(p => Math.max(0, p - 1))}
              >
                ← Prev
              </button>
              <button
                className="btn btn-secondary"
                style={{ marginLeft: '8px' }}
                disabled={(channelPage + 1) * CHANNEL_PAGE_SIZE >= channelTotal}
                onClick={() => setChannelPage(p => p + 1)}
              >
                Next →
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bulk Actions — only for pending view */}
      {!viewingAccepted && !viewingQueue && channels.length > 0 && (
        <div className="control-panel" style={{ marginBottom: '16px' }}>
          <div className="control-row">
            <button className="btn btn-secondary" onClick={selectAll}>
              ☑️ Select All ({channelTotal})
            </button>
            <button className="btn btn-secondary" onClick={deselectAll}>
              ☐ Deselect All
            </button>
            {selectedCount > 0 && (
              <>
                <button className="btn btn-accept" onClick={acceptSelected}>
                  ✅ Accept {selectedCount} Selected
                </button>
                <button className="btn btn-reject" onClick={rejectSelected}>
                  ❌ Reject {selectedCount} Selected
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {/* Channel Grid */}
      {viewingQueue ? null : viewingAccepted ? (
        acceptedChannels.length === 0 ? (
          <div className="empty-state">
            <h2>No accepted channels yet</h2>
            <p>Accept channels from the pending view and they'll show up here</p>
          </div>
        ) : (
          <div className="channel-grid">
            {acceptedChannels.map(ch => (
              <ChannelCard
                key={ch.id}
                channel={ch}
                selected={selectedAcceptedIds.has(ch.id)}
                onToggle={(e) => handleAcceptedCardClick(ch.id, e)}
                onAccept={() => {}}
                onReject={() => {}}
                hideActions={true}
              />
            ))}
          </div>
        )
      ) : channels.length === 0 ? (
        <div className="empty-state">
          <h2>No channels yet</h2>
          <p>Click Start to begin discovering channels</p>
          {running && <div className="spinner" style={{ marginTop: '20px' }} />}
        </div>
      ) : (
        <div className="channel-grid">
          {channels.map(ch => (
            <ChannelCard
              key={ch.id}
              channel={ch}
              selected={selectedIds.has(ch.id)}
              onToggle={(e) => handleCardClick(ch.id, e)}
              onAccept={() => {
                setSelectedIds(new Set([ch.id]));
                acceptSelected();
              }}
              onReject={() => {
                axios.post(`${API}/channels/action`, {
                  channel_ids: [ch.id], action: 'reject'
                }).then(poll);
              }}
            />
          ))}
        </div>
      )}

      {/* Logs */}
      <div className="logs-panel">
        <h3>📝 Live Logs</h3>
        {logs.map((log, i) => (
          <div key={i} className="log-entry">{log}</div>
        ))}
      </div>

      {/* Fixed Action Bar — only for pending view */}
      {!viewingAccepted && selectedCount > 0 && (
        <div className="action-bar">
          <div className="action-bar-left">
            <span><strong>{selectedCount}</strong> channels selected</span>
            <button className="btn btn-accept" onClick={acceptSelected}>
              ✅ Accept All Selected
            </button>
            <button className="btn btn-reject" onClick={rejectSelected}>
              ❌ Reject All Selected
            </button>
          </div>
          <button className="btn btn-secondary" onClick={deselectAll}>
            Clear Selection
          </button>
        </div>
      )}
    </div>
  );
}

export default App;