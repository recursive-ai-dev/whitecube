import React from 'react';
import { useDialog } from './useDialog';

interface ConfirmationModalProps {
  isOpen: boolean;
  title: string;
  message: string;
  command: string;
  onConfirm: () => void;
  onCancel: () => void;
}

const ConfirmationModal: React.FC<ConfirmationModalProps> = ({ 
  isOpen, 
  title, 
  message, 
  command, 
  onConfirm, 
  onCancel 
}) => {
  const dialogRef = useDialog(isOpen, onCancel);
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="confirmation-title" aria-describedby="confirmation-message" tabIndex={-1} className="bg-[#10151e] w-full max-w-md rounded-xl shadow-2xl border border-white/10 overflow-hidden transform transition-all scale-100">
        <div className="bg-whitecube-accent p-4 flex items-center gap-3">
           <div className="bg-white/20 p-2 rounded-full">
             <span className="material-symbols-outlined text-white text-xl">warning</span>
           </div>
           <h3 id="confirmation-title" className="text-white font-bold text-lg">{title}</h3>
        </div>
        
        <div className="p-6">
          <p id="confirmation-message" className="text-white/80 mb-4 text-sm leading-relaxed">{message}</p>
          
          <div className="bg-black/30 rounded p-3 mb-6 border border-white/5">
            <label className="text-[10px] text-white/40 uppercase font-bold tracking-wider mb-1 block">Command Preview</label>
            <code className="text-whitecube-accent font-mono text-xs break-all whitespace-pre-wrap block max-h-56 overflow-auto">{command}</code>
          </div>

          <div className="flex items-center justify-end gap-3">
            <button 
              onClick={onCancel}
              className="px-4 py-2 rounded text-sm font-medium text-white/70 hover:text-white hover:bg-white/10 transition-colors"
            >
              Cancel
            </button>
            <button 
              onClick={onConfirm}
              className="px-6 py-2 rounded text-sm font-bold bg-white/10 border border-white/10 hover:bg-whitecube-accent hover:border-whitecube-accent text-white transition-all shadow-md"
            >
              {title === 'Remove command' ? 'Remove command' : 'Confirm Execution'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ConfirmationModal;
