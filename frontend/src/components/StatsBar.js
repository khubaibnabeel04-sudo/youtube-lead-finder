import React from 'react';

function StatsBar({ stats, onAcceptedClick, viewingAccepted }) {
  const statusClass = stats.status === 'paused' ? 'paused' : stats.status === 'stopped' ? 'stopped' : '';

  return (
    <div className="stats-bar">
      <div className="stat-card">
        <h3>Status</h3>
        <p className={`status ${statusClass}`}>{stats.status}</p>
      </div>
      <div className="stat-card">
        <h3>Keywords Searched</h3>
        <p>{stats.total_searched}</p>
      </div>
      <div className="stat-card">
        <h3>Channels Found</h3>
        <p>{stats.total_found}</p>
      </div>
      <div
        className={`stat-card stat-card-clickable ${viewingAccepted ? 'stat-card-active' : ''}`}
        onClick={onAcceptedClick}
        title="Click to view accepted channels"
      >
        <h3>Accepted</h3>
        <p style={{ color: '#22c55e' }}>{stats.total_accepted}</p>
      </div>
      <div className="stat-card">
        <h3>Batch</h3>
        <p>{stats.current_batch} / {stats.total_batches}</p>
      </div>
    </div>
  );
}

export default StatsBar;