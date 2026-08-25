; =============================================================================
; STAGE 1 — MBR boot sector.
;
; Job: nothing but "load stage2 and jump to it." Real-mode BIOS boot sectors
; are limited to 512 bytes (510 code/data + the 0x55AA signature), which is
; not enough room for a protected-mode transition, a PCI scan, and a hex
; dump — so stage2 lives in the sectors right after this one on the same
; media, and gets loaded via the standard BIOS disk-read interrupt.
; =============================================================================

BITS 16
ORG 0x7C00

start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7C00
    sti

    call serial_init_16
    mov al, 'A'                 ; DEBUG: stage1 alive
    call serial_putchar_16

    mov [boot_drive], dl        ; BIOS hands us the boot drive number in DL
    mov [0x7000], dl            ; also leave it at a fixed, agreed address —
                                 ; stage2 needs it again at the very end, for
                                 ; its own BIOS disk-write call, by which
                                 ; point we're no longer the running code

    mov si, msg_loading
    call print_str16

    mov al, 'L'                 ; DEBUG: about to issue LBA read
    call serial_putchar_16

    ; Load stage2 (32 sectors = 16 KiB) via BIOS extended (LBA) read — not
    ; legacy CHS. CHS read with a fixed sector count doesn't cross track
    ; boundaries reliably (a standard floppy track is only 18 sectors, so
    ; "32 sectors starting at sector 2" runs off the end of the track and
    ; silently reads garbage on hardware/BIOS combinations that don't
    ; auto-advance). LBA sidesteps that entirely, and it's also the correct
    ; mode for the real deployment target: booting off a USB stick, which
    ; BIOS presents as a hard-disk-style device, not a floppy.
    mov si, dap
    mov ah, 0x42
    mov dl, [boot_drive]
    int 0x13
    jc disk_error

    mov al, 'K'                 ; DEBUG: LBA read reported success
    call serial_putchar_16

    mov al, [0x8000]            ; DEBUG: sanity-peek stage2's first loaded byte
    call serial_hex8_16

    mov al, '>'                 ; DEBUG: jumping into stage2
    call serial_putchar_16

    jmp 0x0000:0x8000

; Disk Address Packet for INT 13h/AH=42h (extended read).
dap:
    db 0x10       ; packet size
    db 0          ; reserved
    dw 32         ; sectors to transfer
    dw 0x8000     ; transfer buffer offset
    dw 0x0000     ; transfer buffer segment
    dd 1          ; starting LBA (LBA 0 is this boot sector itself)
    dd 0          ; LBA high dword

disk_error:
    mov al, 'E'                 ; DEBUG: LBA read failed (carry was set)
    call serial_putchar_16
    mov si, msg_error
    call print_str16
.hang:
    cli
    hlt
    jmp .hang

; Minimal BIOS-teletype string printer (real mode only).
print_str16:
    lodsb
    or al, al
    jz .done
    mov ah, 0x0E
    mov bh, 0
    int 0x10
    jmp print_str16
.done:
    ret

; Minimal 16-bit COM1 debug output — same UART, initialized independently
; here so stage1's progress is visible even if stage2 never loads at all.
serial_init_16:
    push ax
    push dx
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 0x80
    out dx, al
    mov dx, 0x3F8
    mov al, 3
    out dx, al
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 0x03
    out dx, al
    mov dx, 0x3FA
    mov al, 0xC7
    out dx, al
    mov dx, 0x3FC
    mov al, 0x0B
    out dx, al
    pop dx
    pop ax
    ret

serial_putchar_16:              ; AL = char
    push ax
    push bx
    push dx
    mov bl, al                  ; stash the character somewhere the poll
                                 ; loop below never touches
.wait:
    mov dx, 0x3FD
    in al, dx
    test al, 0x20
    jz .wait
    mov dx, 0x3F8
    mov al, bl                  ; recover the character, now that AL is free
    out dx, al
    pop dx
    pop bx
    pop ax
    ret

serial_hex8_16:                 ; AL = byte, prints 2 hex digits
    push ax
    push ax
    shr al, 4
    call .nib
    pop ax
    and al, 0x0F
    call .nib
    pop ax
    ret
.nib:
    cmp al, 10
    jl .d
    add al, 'A'-10
    jmp .o
.d:
    add al, '0'
.o:
    call serial_putchar_16
    ret

boot_drive:  db 0
msg_loading: db 'REMOTE_AGENT: loading stage2...', 13, 10, 0
msg_error:   db 'REMOTE_AGENT: DISK READ ERROR', 13, 10, 0

times 510-($-$$) db 0
dw 0xAA55
