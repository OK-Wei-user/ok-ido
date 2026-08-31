'use client'

import { useRouter } from 'next/navigation'
import { Sidebar, SidebarContent, SidebarFooter, SidebarHeader, SidebarTrigger } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { Plus, LogOut, User } from 'lucide-react'
import { Kbd, KbdGroup } from '@/components/ui/kbd'
import { SessionList } from '@/components/session-list'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Separator } from '@/components/ui/separator'
import { useAuth } from '@/providers/auth-provider'

export function LeftPanel() {
  const router = useRouter()
  const { user, logout } = useAuth()

  const handleLogout = () => {
    logout()
  }

  return (
    <Sidebar>
      <SidebarHeader>
        <SidebarTrigger className="cursor-pointer"/>
      </SidebarHeader>
      <SidebarContent className="p-2">
        <Button
          variant="outline"
          className="cursor-pointer mb-3"
          onClick={() => router.push('/')}
        >
          <Plus/>
          新建任务
          <KbdGroup>
            <Kbd>⌘</Kbd>
            <Kbd>K</Kbd>
          </KbdGroup>
        </Button>
        <SessionList/>
      </SidebarContent>
      <SidebarFooter className="p-2">
        <Separator className="mb-2" />
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Avatar className="h-8 w-8">
              <AvatarFallback>
                <User className="h-4 w-4" />
              </AvatarFallback>
            </Avatar>
            <div className="flex flex-col">
              <span className="text-sm font-medium">{user?.username || '用户'}</span>
              <span className="text-xs text-gray-500">{user?.phone || ''}</span>
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={handleLogout}
            className="cursor-pointer"
          >
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </SidebarFooter>
    </Sidebar>
  )
}
