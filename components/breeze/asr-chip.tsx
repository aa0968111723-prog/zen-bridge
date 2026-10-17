'use client';
import {Cpu,Zap,TriangleAlert} from 'lucide-react';
import {asrChip,type GpuStatus} from '@/lib/breeze-live';

export default function AsrChip({gpu,mode,compact=false}:{gpu?:GpuStatus|null;mode?:string;compact?:boolean}){
 const chip=asrChip(gpu,mode);
 const Icon=chip.tone==='ready'?Zap:chip.tone==='warn'?TriangleAlert:Cpu;
 return <span className={'asr-chip tone-'+chip.tone+(compact?' compact':'')} title={chip.detail} role="status"><Icon size={compact?13:15}/><span>{chip.label}</span>{!compact&&<small>{chip.detail}</small>}</span>;
}
