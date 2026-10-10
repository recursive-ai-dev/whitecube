import React from 'react';
export default class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: Error) { return { error: error.message }; }
  render() {
    if (!this.state.error) return this.props.children;
    return <main className="min-h-screen p-12 bg-[#04070d] text-white"><h1 className="text-2xl mb-4">Whitecube Command Center could not render this view</h1><p className="text-white/60 mb-6">{this.state.error}</p><button className="bg-white/10 rounded-lg px-4 py-2" onClick={() => location.reload()}>Reload application</button></main>;
  }
}
