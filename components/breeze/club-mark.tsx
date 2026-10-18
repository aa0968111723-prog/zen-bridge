/** 龜龜 + 小華 pair: the club's own art used as the Breeze cards' header mark (no separate logo). */
export default function ClubMark({size=48,className=''}:{size?:number;className?:string}){
 return <span className={'club-mark '+className} style={{height:size,width:Math.round(size*1.55)}} role="img" aria-label="龜龜與小華">
  <img className="club-mark-girl" src="/art/poster-girl-180.webp" alt="" width={size*0.88} height={size} decoding="async"/>
  <img className="club-mark-turtle" src="/art/turtle-logo-128.webp" alt="" width={size*0.7} height={size*0.84} decoding="async"/>
 </span>;
}
