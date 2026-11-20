import { useEffect, useMemo, useState } from 'react'
import toast from 'react-hot-toast'
import { updateCustomerIssue } from '../services/api'
import { useData } from '../context/DataContext'

function formatDar(issue) {
  const raw = issue?.dar_percent ?? issue?.defect_area_ratio
  if (raw === null || raw === undefined || raw === '') {
    return { percent: 0, label: 'DAR not recorded', recorded: false }
  }
  const number = Number(raw)
  if (!Number.isFinite(number) || number < 0) {
    return { percent: 0, label: 'DAR not recorded', recorded: false }
  }
  const percent = number <= 1 ? number * 100 : number
  const rounded = Math.round(percent * 10) / 10
  return { percent: rounded, label: `${rounded}% DAR`, recorded: true }
}

function formatAgentDecision(issue) {
  const raw = (issue?.agent_decision || issue?.suggested_action || issue?.status || '').toString().trim()
  const key = raw.toLowerCase().replace(/[\s-]+/g, '_')
  const labels = {
    hold_refund_until_received: 'Hold refund until return received',
    refund_pending_return: 'Hold refund until return received',
    return_received_awaiting_refund_agent: 'Product received — refund agent paying',
    refund_initiated: 'Refund initiated at policy allowance',
    options_presented: 'Options presented to customer',
    escalate_no_defect: 'Escalate: no visible defect',
    no_defect_detected_escalated: 'Escalate: no visible defect',
    under_review: 'Manual support review',
    exchange_pending: 'Exchange pending',
    await_returned_product: 'Hold refund until return received',
    policy_refund_allowance: 'Refund initiated at policy allowance',
    full_refund: 'Refund initiated at policy allowance',
    instant_replacement_dispatch: 'Exchange pending',
  }
  return labels[key] || raw.replace(/_/g, ' ') || 'Decision pending'
}

function SeverityBadge({ severity }) {
  const s = (severity || '').toString().trim().toLowerCase()
  if (!s || s === 'none' || s === 'null' || s === 'undefined') {
    return (
      <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
        Severity not assessed
      </span>
    )
  }
  if (s.includes('severe') || s.includes('unusable') || s.includes('high') || s.includes('urgent')) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2.5 py-0.5 text-xs font-semibold text-rose-700 ring-1 ring-inset ring-rose-600/20">
        <span className="h-1.5 w-1.5 rounded-full bg-rose-600 animate-pulse" />
        Severe / Unusable
      </span>
    )
  }
  if (s.includes('moderate') || s.includes('functional') || s.includes('medium')) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-semibold text-amber-700 ring-1 ring-inset ring-amber-600/20">
        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
        Moderate Defect
      </span>
    )
  }
  if (s.includes('minor') || s.includes('cosmetic') || s.includes('low')) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-semibold text-blue-700 ring-1 ring-inset ring-blue-700/10">
        <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
        Minor Cosmetic
      </span>
    )
  }
  return (
    <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium capitalize text-slate-700">
      {s.replace(/_/g, ' ')}
    </span>
  )
}

function StatusBadge({ status, agentDecision }) {
  const st = (status || 'open').toLowerCase()
  const decision = (agentDecision || '').toLowerCase().replace(/[\s-]+/g, '_')
  if (st === 'refund_initiated' || decision === 'refund_initiated') {
    if (st === 'resolved' || st === 'closed') {
      return (
        <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-800 ring-1 ring-inset ring-emerald-600/20">
          Resolved · Refund initiated
        </span>
      )
    }
    return (
      <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-800 ring-1 ring-inset ring-emerald-600/20">
        Refund Initiated
      </span>
    )
  }
  if (st === 'exchange_pending') {
    return (
      <span className="inline-flex items-center rounded-md bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-800 ring-1 ring-inset ring-indigo-600/20">
        Exchange Pending
      </span>
    )
  }
  if (st === 'options_presented') {
    return (
      <span className="inline-flex items-center rounded-md bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-800 ring-1 ring-inset ring-amber-600/20">
        Options Presented
      </span>
    )
  }
  if (st === 'received') {
    return (
      <span className="inline-flex items-center rounded-md bg-cyan-50 px-2.5 py-1 text-xs font-semibold text-cyan-800 ring-1 ring-inset ring-cyan-600/20">
        Product Received
      </span>
    )
  }
  if (st === 'resolved' || st === 'closed') {
    return (
      <span className="inline-flex items-center rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700">
        Resolved
      </span>
    )
  }
  return (
    <span className="inline-flex items-center rounded-md bg-rose-50 px-2.5 py-1 text-xs font-medium text-rose-700 ring-1 ring-inset ring-rose-600/20">
      Open Dispute
    </span>
  )
}

export default function OpenIssuesPage() {
  const { issues, loadingIssues, refreshIssues } = useData()
  const [selectedStatus, setSelectedStatus] = useState('all')
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedIssue, setSelectedIssue] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)

  useEffect(() => {
    refreshIssues()
    const interval = setInterval(refreshIssues, 25000)
    return () => clearInterval(interval)
  }, [refreshIssues])

  const filteredIssues = useMemo(() => {
    return (issues || []).filter((item) => {
      const status = (item.status || '').toLowerCase()
      const decision = (item.agent_decision || '').toLowerCase().replace(/[\s-]+/g, '_')
      const matchStatus =
        selectedStatus === 'all' ||
        status === selectedStatus.toLowerCase() ||
        (selectedStatus === 'refund_initiated' && decision === 'refund_initiated')
      const q = searchQuery.toLowerCase()
      const matchQuery =
        !q ||
        (item.issue_title || '').toLowerCase().includes(q) ||
        (item.customer_email || '').toLowerCase().includes(q) ||
        (item.order_number || '').toLowerCase().includes(q) ||
        (item.defect_type || '').toLowerCase().includes(q)
      return matchStatus && matchQuery
    })
  }, [issues, selectedStatus, searchQuery])

  const stats = useMemo(() => {
    const total = (issues || []).length
    const refundInit = (issues || []).filter(
      (i) =>
        i.status === 'refund_initiated' ||
        (i.agent_decision || '').toLowerCase().replace(/[\s-]+/g, '_') === 'refund_initiated',
    ).length
    const exchangePend = (issues || []).filter((i) => i.status === 'exchange_pending').length
    const optionsPres = (issues || []).filter((i) => i.status === 'options_presented').length
    const dars = (issues || []).map((i) => formatDar(i)).filter((d) => d.recorded)
    const avgDar = dars.length ? dars.reduce((a, b) => a + b.percent, 0) / dars.length : 0

    return { total, refundInit, exchangePend, optionsPres, avgDar: Math.round(avgDar) }
  }, [issues])

  const handleResolve = async (issueId) => {
    setActionLoading(true)
    try {
      await updateCustomerIssue(issueId, { status: 'resolved', resolution_notes: 'Resolved by Admin' })
      toast.success('Issue marked as resolved')
      refreshIssues()
      if (selectedIssue?.id === issueId) setSelectedIssue(null)
    } catch (err) {
      toast.error('Failed to update issue status')
    } finally {
      setActionLoading(false)
    }
  }

  const handleMarkReceived = async (issue) => {
    setActionLoading(true)
    try {
      await updateCustomerIssue(issue.id, {
        status: 'received',
        resolution_notes: 'Returned product physically received and ready for refund processing.',
      })
      toast.success(`Product received for ${issue.order_number || 'order'}`)
      refreshIssues()
      if (selectedIssue?.id === issue.id) setSelectedIssue(null)
    } catch (err) {
      toast.error('Failed to mark product as received')
    } finally {
      setActionLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Open Customer Issues & Defect Triage</h1>
          <p className="text-sm text-slate-500">
            Real-time multimodal defect inspection, inventory availability checks, and autonomous refund/exchange resolutions.
          </p>
        </div>
        <button
          onClick={() => refreshIssues()}
          disabled={loadingIssues}
          className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
        >
          {loadingIssues ? 'Refreshing...' : 'Refresh Issues'}
        </button>
      </div>

      {/* Metric Cards */}
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Total Tracked Issues</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">{stats.total}</p>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-wider text-amber-600">Options Presented</p>
          <p className="mt-2 text-3xl font-bold text-amber-700">{stats.optionsPres}</p>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-wider text-emerald-600">Refunds Initiated</p>
          <p className="mt-2 text-3xl font-bold text-emerald-700">{stats.refundInit}</p>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-wider text-indigo-600">Exchanges Pending</p>
          <p className="mt-2 text-3xl font-bold text-indigo-700">{stats.exchangePend}</p>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Avg Defect Area (DAR)</p>
          <p className="mt-2 text-3xl font-bold text-slate-900">{stats.avgDar}%</p>
        </div>
      </section>

      {/* Filters & Search */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between rounded-xl border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap gap-2">
          {[
            { key: 'all', label: 'All Issues' },
            { key: 'options_presented', label: 'Options Presented' },
            { key: 'received', label: 'Products Received' },
            { key: 'refund_initiated', label: 'Refunds Initiated' },
            { key: 'exchange_pending', label: 'Exchanges Pending' },
            { key: 'resolved', label: 'Resolved' },
          ].map((tab) => (
            <button
              key={tab.key}
              onClick={() => setSelectedStatus(tab.key)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                selectedStatus === tab.key
                  ? 'bg-slate-900 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="w-full sm:w-72">
          <input
            type="text"
            placeholder="Search by customer, order, defect..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm placeholder-slate-400 focus:border-slate-900 focus:outline-none"
          />
        </div>
      </div>

      {/* Issues Table */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {loadingIssues && issues.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">Loading issues...</div>
        ) : filteredIssues.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-base font-semibold text-slate-700">No issues found</p>
            <p className="mt-1 text-sm text-slate-400">
              When customers report damages, defects, or request refunds, issues will be automatically triaged and listed here.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="px-4 py-3.5">Customer & Order</th>
                  <th className="px-4 py-3.5">Issue & Defect Triage</th>
                  <th className="px-4 py-3.5">Severity</th>
                  <th className="px-4 py-3.5">DAR</th>
                  <th className="px-4 py-3.5">Agent Decision</th>
                  <th className="px-4 py-3.5">Status</th>
                  <th className="px-4 py-3.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 bg-white">
                {filteredIssues.map((issue) => {
                  const dar = formatDar(issue)
                  const barWidth = dar.recorded ? Math.min(100, Math.max(4, dar.percent)) : 0
                  return (
                    <tr key={issue.id} className="hover:bg-slate-50/75 transition-colors">
                      {/* Customer & Order */}
                      <td className="px-4 py-3.5">
                        <div className="font-semibold text-slate-900">{issue.customer_name || 'Customer'}</div>
                        <div className="text-xs text-slate-500">{issue.customer_email}</div>
                        {issue.order_number && (
                          <span className="mt-1 inline-block rounded bg-slate-100 px-2 py-0.5 text-xs font-mono font-medium text-slate-700">
                            {issue.order_number}
                          </span>
                        )}
                      </td>

                      {/* Issue & Defect */}
                      <td className="px-4 py-3.5">
                        <div className="font-medium text-slate-900">{issue.issue_title}</div>
                        <div className="text-xs text-slate-500 max-w-xs truncate">{issue.description}</div>
                        <span className="mt-1 inline-flex items-center rounded-full bg-purple-50 px-2 py-0.5 text-xs font-medium text-purple-700">
                          {(issue.defect_type && issue.defect_type !== 'none' ? issue.defect_type : 'no_defect_recorded').replaceAll('_', ' ')}
                        </span>
                      </td>

                      {/* Severity */}
                      <td className="px-4 py-3.5">
                        <SeverityBadge severity={issue.severity} />
                      </td>

                      {/* DAR */}
                      <td className="px-4 py-3.5">
                        <div className="min-w-[140px] space-y-1">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">DAR</span>
                            <span className={`text-sm font-bold ${dar.recorded ? 'text-slate-900' : 'text-slate-400'}`}>
                              {dar.recorded ? `${dar.percent}%` : 'N/A'}
                            </span>
                          </div>
                          <div className="h-1.5 w-full rounded-full bg-slate-200 overflow-hidden">
                            <div
                              className={`h-full ${
                                !dar.recorded
                                  ? 'bg-slate-300'
                                  : dar.percent > 25
                                    ? 'bg-rose-500'
                                    : dar.percent > 10
                                      ? 'bg-amber-500'
                                      : 'bg-blue-500'
                              }`}
                              style={{ width: `${barWidth}%` }}
                            />
                          </div>
                          <div className="text-[11px] text-slate-500">{dar.recorded ? dar.label : 'No visual DAR stored'}</div>
                        </div>
                      </td>

                      {/* Agent Decision */}
                      <td className="px-4 py-3.5">
                        <div className="text-xs font-semibold text-slate-900">
                          {formatAgentDecision(issue)}
                        </div>
                        {issue.suggested_action && (
                          <div className="text-[11px] text-slate-500 max-w-xs truncate mt-0.5">
                            Plan: {issue.suggested_action}
                          </div>
                        )}
                        {issue.resolution_notes && (
                          <div className="text-xs text-slate-500 max-w-xs truncate mt-0.5">
                            {issue.resolution_notes}
                          </div>
                        )}
                      </td>

                      {/* Status */}
                      <td className="px-4 py-3.5">
                        <StatusBadge status={issue.status} agentDecision={issue.agent_decision} />
                      </td>

                      {/* Actions */}
                      <td className="px-4 py-3.5 text-right space-x-1.5 whitespace-nowrap">
                        <button
                          onClick={() => setSelectedIssue(issue)}
                          className="rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200"
                        >
                          View
                        </button>
                        {!['received', 'refund_initiated', 'resolved'].includes(issue.status) && (
                          <button
                            onClick={() => handleMarkReceived(issue)}
                            disabled={actionLoading}
                            className="rounded-md bg-cyan-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-cyan-700 disabled:opacity-50"
                          >
                            Mark Received
                          </button>
                        )}
                        {issue.status !== 'resolved' && (
                          <button
                            onClick={() => handleResolve(issue.id)}
                            disabled={actionLoading}
                            className="rounded-md bg-slate-800 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-900 disabled:opacity-50"
                          >
                            Resolve
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Details Modal */}
      {selectedIssue && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between border-b pb-3">
              <h3 className="text-lg font-bold text-slate-900">Issue Details & Triage Audit</h3>
              <button
                onClick={() => setSelectedIssue(null)}
                className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                ✕
              </button>
            </div>

            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-slate-500">Customer:</span>
                <span className="font-semibold text-slate-900">{selectedIssue.customer_name} ({selectedIssue.customer_email})</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Order ID:</span>
                <span className="font-mono font-medium text-slate-900">{selectedIssue.order_number || 'N/A'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Defect Type:</span>
                <span className="font-medium capitalize text-purple-700">{selectedIssue.defect_type && selectedIssue.defect_type !== 'none' ? selectedIssue.defect_type.replaceAll('_', ' ') : 'Not recorded'}</span>
              </div>
              <div className="flex justify-between items-center gap-3">
                <span className="text-slate-500">Severity:</span>
                <SeverityBadge severity={selectedIssue.severity} />
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Defect Area Ratio (DAR):</span>
                <span className="font-semibold text-slate-900">
                  {formatDar(selectedIssue).recorded ? `${formatDar(selectedIssue).percent}% of the defect area visible` : 'DAR not recorded'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Agent Decision:</span>
                <span className="font-semibold text-right text-slate-900">{formatAgentDecision(selectedIssue)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Current Status:</span>
                <StatusBadge status={selectedIssue.status} agentDecision={selectedIssue.agent_decision} />
              </div>

              <div className="border-t pt-3">
                <p className="text-xs font-semibold text-slate-500 uppercase">Issue Description</p>
                <p className="mt-1 text-slate-700 bg-slate-50 rounded-lg p-2.5 text-xs">{selectedIssue.description || 'No description recorded.'}</p>
              </div>

              <div>
                <p className="text-xs font-semibold text-slate-500 uppercase">Resolution Notes & AI Action Plan</p>
                <p className="mt-1 text-slate-700 bg-emerald-50 rounded-lg p-2.5 text-xs font-medium text-emerald-900">
                  {selectedIssue.resolution_notes || selectedIssue.suggested_action || 'Pending resolution action.'}
                </p>
              </div>
            </div>

            <div className="flex justify-end gap-2 border-t pt-4">
              <button
                onClick={() => setSelectedIssue(null)}
                className="rounded-lg border border-slate-300 px-4 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                Close
              </button>
              {selectedIssue.status !== 'resolved' && (
                <button
                  onClick={() => handleResolve(selectedIssue.id)}
                  disabled={actionLoading}
                  className="rounded-lg bg-slate-900 px-4 py-2 text-xs font-medium text-white hover:bg-slate-800"
                >
                  Mark Resolved
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
