/** 禪釋 lotus mark (sidecar/live-room brand). Shown beside 龜龜, never instead of it. */
export default function ZenShiMark({size=34,className=''}:{size?:number;className?:string}){
 return <img src={size>64?'/art/zen-shi-logo-256.png':'/art/zen-shi-logo-128.png'} alt="禪釋" width={size} height={size} decoding="async" className={'zen-shi-mark '+className}/>;
}
