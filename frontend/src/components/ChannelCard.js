import React from 'react';

function ChannelCard({ channel, selected, onToggle, onAccept, onReject, hideActions }) {
  const formatSubs = (num) => {
    if (num === null || num === undefined || num === '') return 'N/A';
    const n = Number(num);
    if (isNaN(n)) return 'N/A';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toLocaleString();
  };

  const formatDate = (dateStr) => {
    if (!dateStr || typeof dateStr !== 'string') return 'Unknown';
    if (dateStr.length === 8) {
      return dateStr.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3');
    }
    return dateStr;
  };

  const handleLinkClick = (e) => e.stopPropagation();

  const name        = channel?.name        || 'Unknown Channel';
  const url         = channel?.url         || '#';
  const subscribers = channel?.subscribers;
  const uploadDate  = channel?.uploadDate;
  const description = channel?.description || 'No description available';
  const keyword     = channel?.keyword || channel?.source || 'unknown';
  const thumbnail   = channel?.thumbnail;
  const isBorderline = channel?.borderline === true;
  const nicheScore  = channel?.niche_score ?? null;

  return (
    <div
      className={`channel-card ${selected ? 'selected' : ''} ${isBorderline ? 'borderline' : ''}`}
      onClick={onToggle}
      onMouseDown={(e) => { if (e.shiftKey) e.preventDefault(); }}
      style={isBorderline ? { borderColor: '#f59e0b', opacity: 0.9 } : {}}
    >
      <div className="checkbox" />

      {/* Borderline badge */}
      {isBorderline && (
        <div style={{
          position: 'absolute',
          top: '10px',
          right: '10px',
          background: '#f59e0b',
          color: '#000',
          fontSize: '10px',
          fontWeight: '700',
          padding: '2px 8px',
          borderRadius: '4px',
          letterSpacing: '0.05em',
        }}>
          ⚠️ REVIEW — low niche score ({nicheScore})
        </div>
      )}

      <div className="card-header">
        {thumbnail ? (
          <a href={url} target="_blank" rel="noopener noreferrer" onClick={handleLinkClick}>
            <img src={thumbnail} alt={name} className="thumbnail" />
          </a>
        ) : (
          <a href={url} target="_blank" rel="noopener noreferrer" onClick={handleLinkClick}
             className="thumbnail-placeholder"
             style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            📺
          </a>
        )}
        <div className="card-info">
          <h3>
            <a href={url} target="_blank" rel="noopener noreferrer" onClick={handleLinkClick}
               style={{ color: '#fafafa', textDecoration: 'none' }}
               onMouseEnter={(e) => e.target.style.color = '#22c55e'}
               onMouseLeave={(e) => e.target.style.color = '#fafafa'}>
              {name}
            </a>
          </h3>
          <p className="subs">{formatSubs(subscribers)} subscribers</p>
          <p className="meta">📅 {formatDate(uploadDate)}</p>
        </div>
      </div>

      <p className="card-description">{description}</p>

      <div className="card-footer">
        <span className="keyword-tag" style={hideActions ? { background: '#052e16', color: '#22c55e', border: '1px solid #22c55e' } : {}}>
          {hideActions ? '✅ Saved' : (isBorderline ? '⚠️ borderline' : `🔑 ${keyword}`)}
        </span>
        {!hideActions && (
          <div className="card-actions">
            <button
              className="btn-small btn-accept"
              onClick={(e) => { e.stopPropagation(); onAccept(); }}
            >
              ✅ Accept
            </button>
            <button
              className="btn-small btn-reject"
              onClick={(e) => { e.stopPropagation(); onReject(); }}
            >
              ❌ Reject
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default ChannelCard;