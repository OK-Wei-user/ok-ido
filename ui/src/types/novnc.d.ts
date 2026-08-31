/**
 * @novnc/novnc 主入口类型声明
 *
 * 官方 @types/novnc__novnc 仅声明了子路径模块 @novnc/novnc/lib/rfb，
 * 但运行时包的主入口 @novnc/novnc 导出 RFB 类（NoVncClient 的旧名称）。
 * 此声明补齐主入口的类型，使 TypeScript 编译通过。
 */
declare module '@novnc/novnc' {
  import NoVncClient from '@novnc/novnc/lib/rfb'
  export default NoVncClient
}
